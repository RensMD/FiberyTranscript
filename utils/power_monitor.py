"""Platform-specific system sleep/wake detection.

Detects when the OS is about to suspend (lid close, sleep) and when it
resumes, so the app can gracefully save recordings and notify the user.
"""

import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


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
    """Windows: hidden message-only window receiving WM_POWERBROADCAST."""

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run_message_loop, daemon=True, name="power-monitor"
        )
        self._thread.start()

    def _run_message_loop(self) -> None:
        import ctypes
        import ctypes.wintypes as wt

        # Win32 constants
        WM_POWERBROADCAST = 0x0218
        PBT_APMSUSPEND = 0x0004
        PBT_APMRESUMESUSPEND = 0x0007
        PBT_APMRESUMEAUTOMATIC = 0x0012
        WM_QUIT = 0x0012
        HWND_MESSAGE = ctypes.c_void_p(-3)

        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t

        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT,          # LRESULT
            ctypes.c_void_p,  # HWND
            ctypes.c_uint,    # UINT msg
            WPARAM,           # WPARAM
            LPARAM,           # LPARAM
        )

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Set proper types for DefWindowProcW to avoid overflow on 64-bit
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                wp = int(wparam) if wparam else 0
                if wp == PBT_APMSUSPEND:
                    logger.info("System going to sleep (PBT_APMSUSPEND)")
                    try:
                        self._on_sleep()
                    except Exception:
                        logger.error("on_sleep callback failed", exc_info=True)
                elif wp in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    logger.info("System waking up (PBT=0x%04X)", wp)
                    try:
                        self._on_wake()
                    except Exception:
                        logger.error("on_wake callback failed", exc_info=True)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        # prevent GC of the callback
        self._wnd_proc_ref = WNDPROC(wnd_proc)

        # WNDCLASSEXW struct
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

        logger.info("Power monitor started (hwnd=%s)", hwnd)
        self._hwnd = hwnd

        # Message pump
        msg = wt.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, hinstance)
        logger.info("Power monitor stopped")

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            try:
                import ctypes
                # Post WM_QUIT to unblock GetMessageW
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread.ident, 0x0012, 0, 0
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
