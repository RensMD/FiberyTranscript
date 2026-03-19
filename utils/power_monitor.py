"""Platform-specific system sleep/wake detection.

Detects when the OS is about to suspend (lid close, sleep) and when it
resumes, so the app can gracefully save recordings and notify the user.

On Windows, uses PowerRegisterSuspendResumeNotification (direct callback)
as the primary mechanism — this works on both traditional S3 sleep AND
Modern Standby (S0 low-power idle) systems. Falls back to a message-only
window with RegisterSuspendResumeNotification if the primary API fails.

The old approach (message-only window without explicit registration) was
broken on Modern Standby because HWND_MESSAGE windows do not receive
broadcast messages like WM_POWERBROADCAST.
"""

import logging
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Win32 power broadcast constants (shared across mechanisms)
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012


class PowerMonitor:
    """Base class for platform-specific power event detection."""

    def __init__(
        self,
        on_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        self._on_sleep = on_sleep
        self._on_wake = on_wake
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._running = False


class WindowsPowerMonitor(PowerMonitor):
    """Windows sleep/wake detection.

    Primary: PowerRegisterSuspendResumeNotification with DEVICE_NOTIFY_CALLBACK.
    This registers a direct callback invoked on a system thread pool thread,
    and works reliably on both S3 and Modern Standby systems.

    Fallback: message-only window with RegisterSuspendResumeNotification
    (explicit opt-in to WM_POWERBROADCAST delivery).
    """

    def __init__(self, on_sleep, on_wake) -> None:
        super().__init__(on_sleep, on_wake)
        self._registration_handle = None
        self._callback_ref = None  # prevent GC of ctypes callback
        self._params_ref = None    # prevent GC of params struct
        self._stop_event = threading.Event()
        # Dedup: prevent double-firing if both mechanisms are active
        self._last_sleep_time: float = 0.0
        self._last_wake_time: float = 0.0
        self._DEDUP_WINDOW = 5.0  # seconds

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="power-monitor"
        )
        self._thread.start()

    def _fire_sleep(self, source: str) -> None:
        """Fire sleep callback with deduplication."""
        now = time.monotonic()
        if now - self._last_sleep_time < self._DEDUP_WINDOW:
            logger.debug("Dedup: ignoring duplicate sleep event from %s", source)
            return
        self._last_sleep_time = now
        logger.info("System going to sleep (%s)", source)
        try:
            self._on_sleep()
        except Exception:
            logger.error("on_sleep callback failed", exc_info=True)

    def _fire_wake(self, source: str, event_type: int) -> None:
        """Fire wake callback with deduplication."""
        now = time.monotonic()
        if now - self._last_wake_time < self._DEDUP_WINDOW:
            logger.debug("Dedup: ignoring duplicate wake event from %s", source)
            return
        self._last_wake_time = now
        logger.info("System waking up (%s, PBT=0x%04X)", source, event_type)
        try:
            self._on_wake()
        except Exception:
            logger.error("on_wake callback failed", exc_info=True)

    def _run(self) -> None:
        """Try primary (direct callback), fall back to message-only window."""
        if self._register_power_callback():
            logger.info("Power monitor started (PowerRegisterSuspendResumeNotification)")
            # Callback runs on system thread pool — just keep this thread alive
            self._stop_event.wait()
            self._unregister_power_callback()
            logger.info("Power monitor stopped")
            return

        # Fallback: message-only window
        logger.info("Direct callback registration failed, falling back to window method")
        self._run_message_loop()

    def _register_power_callback(self) -> bool:
        """Register via PowerRegisterSuspendResumeNotification (Win8+).

        Returns True on success.
        """
        import ctypes

        DEVICE_NOTIFY_CALLBACK = 2

        # Callback type: ULONG CALLBACK(PVOID Context, ULONG Type, PVOID Setting)
        CALLBACK_TYPE = ctypes.WINFUNCTYPE(
            ctypes.c_ulong,   # return
            ctypes.c_void_p,  # Context
            ctypes.c_ulong,   # Type (PBT_*)
            ctypes.c_void_p,  # Setting (unused for suspend/resume)
        )

        def power_callback(context, event_type, setting):
            if event_type == PBT_APMSUSPEND:
                self._fire_sleep("PowerCallback")
            elif event_type in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                self._fire_wake("PowerCallback", event_type)
            return 0  # ERROR_SUCCESS

        # prevent GC
        self._callback_ref = CALLBACK_TYPE(power_callback)

        # DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS struct
        class DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS(ctypes.Structure):
            _fields_ = [
                ("Callback", CALLBACK_TYPE),
                ("Context", ctypes.c_void_p),
            ]

        params = DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS()
        params.Callback = self._callback_ref
        params.Context = None
        self._params_ref = params  # prevent GC

        self._registration_handle = ctypes.c_void_p()

        try:
            powrprof = ctypes.windll.LoadLibrary("powrprof.dll")
            result = powrprof.PowerRegisterSuspendResumeNotification(
                ctypes.c_ulong(DEVICE_NOTIFY_CALLBACK),
                ctypes.byref(params),
                ctypes.byref(self._registration_handle),
            )
            if result == 0:  # ERROR_SUCCESS
                return True
            logger.warning(
                "PowerRegisterSuspendResumeNotification failed (error=%d)", result
            )
            return False
        except Exception as e:
            logger.warning(
                "PowerRegisterSuspendResumeNotification unavailable: %s", e
            )
            return False

    def _unregister_power_callback(self) -> None:
        if self._registration_handle:
            try:
                import ctypes
                ctypes.windll.LoadLibrary("powrprof.dll").PowerUnregisterSuspendResumeNotification(
                    self._registration_handle
                )
            except Exception:
                logger.debug("Error unregistering power callback", exc_info=True)
            self._registration_handle = None

    def _run_message_loop(self) -> None:
        """Fallback: message-only window with explicit RegisterSuspendResumeNotification."""
        import ctypes
        import ctypes.wintypes as wt

        WM_POWERBROADCAST = 0x0218
        WM_QUIT = 0x0012
        HWND_MESSAGE = ctypes.c_void_p(-3)
        DEVICE_NOTIFY_WINDOW_HANDLE = 0

        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t

        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_void_p, ctypes.c_uint, WPARAM, LPARAM,
        )

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                wp = int(wparam) if wparam else 0
                if wp == PBT_APMSUSPEND:
                    self._fire_sleep("WndProc")
                elif wp in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    self._fire_wake("WndProc", wp)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_ref = WNDPROC(wnd_proc)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p),
            ]

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "FiberyTranscriptPowerMonitor"

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wnd_proc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = class_name

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            logger.error("RegisterClassExW failed for power monitor")
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, "PowerMonitor", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, hinstance, None,
        )
        if not hwnd:
            logger.error("CreateWindowExW failed for power monitor")
            user32.UnregisterClassW(class_name, hinstance)
            return

        # Explicitly register for suspend/resume notifications.
        # Message-only windows (HWND_MESSAGE) don't receive broadcast messages,
        # so this is required for WM_POWERBROADCAST delivery.
        suspend_notify_handle = None
        try:
            user32.RegisterSuspendResumeNotification.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong
            ]
            user32.RegisterSuspendResumeNotification.restype = ctypes.c_void_p
            suspend_notify_handle = user32.RegisterSuspendResumeNotification(
                hwnd, DEVICE_NOTIFY_WINDOW_HANDLE,
            )
            if suspend_notify_handle:
                logger.info("RegisterSuspendResumeNotification OK (hwnd=%s)", hwnd)
            else:
                logger.warning("RegisterSuspendResumeNotification returned NULL")
        except Exception as e:
            logger.warning("RegisterSuspendResumeNotification failed: %s", e)

        logger.info("Power monitor started (message-only window, hwnd=%s)", hwnd)
        self._hwnd = hwnd

        # Message pump
        msg = wt.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup
        if suspend_notify_handle:
            try:
                user32.UnregisterSuspendResumeNotification(suspend_notify_handle)
            except Exception:
                logger.debug("Error unregistering suspend notification", exc_info=True)
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, hinstance)
        logger.info("Power monitor stopped")

    def stop(self) -> None:
        self._running = False
        # Unblock the primary mechanism's wait
        self._stop_event.set()
        # Unblock the fallback message pump
        if self._thread and self._thread.is_alive():
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread.ident, 0x0012, 0, 0  # WM_QUIT
                )
                self._thread.join(timeout=3.0)
            except Exception:
                logger.debug("Error stopping power monitor thread", exc_info=True)


class _StubPowerMonitor(PowerMonitor):
    """Placeholder for platforms without power monitoring yet."""

    def start(self) -> None:
        logger.info("Power monitoring not implemented for this platform")

    def stop(self) -> None:
        pass


def create_power_monitor(
    on_sleep: Callable[[], None],
    on_wake: Callable[[], None],
) -> Optional[PowerMonitor]:
    """Create the appropriate PowerMonitor for the current platform."""
    if sys.platform == "win32":
        return WindowsPowerMonitor(on_sleep, on_wake)
    elif sys.platform == "darwin":
        logger.info("macOS power monitoring not yet implemented")
        return _StubPowerMonitor(on_sleep, on_wake)
    else:
        logger.info("Linux power monitoring not yet implemented")
        return _StubPowerMonitor(on_sleep, on_wake)
