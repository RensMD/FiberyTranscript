"""Best-effort single-instance guard for desktop startup."""

from __future__ import annotations

import logging
import sys

from config.constants import APP_SINGLE_INSTANCE_MUTEX_NAME, APP_WINDOW_TITLE

logger = logging.getLogger(__name__)

_WINDOWS_MUTEX_ALREADY_EXISTS = object()


class SingleInstanceGuard:
    """Keeps the underlying OS guard alive for the current process."""

    def __init__(self, handle=None, releaser=None):
        self._handle = handle
        self._releaser = releaser

    def release(self) -> None:
        """Release the guard. Safe to call multiple times."""
        if self._handle is None or self._releaser is None:
            return

        handle = self._handle
        self._handle = None
        try:
            self._releaser(handle)
        except Exception:
            logger.debug("Failed to release single-instance guard", exc_info=True)


def acquire_single_instance_guard(
    mutex_name: str = APP_SINGLE_INSTANCE_MUTEX_NAME,
    window_title: str = APP_WINDOW_TITLE,
) -> SingleInstanceGuard | None:
    """Return a guard for this process, or None if another instance is already running."""
    if sys.platform != "win32":
        return SingleInstanceGuard()

    handle = _create_windows_mutex(mutex_name)
    if handle is _WINDOWS_MUTEX_ALREADY_EXISTS:
        logger.info("Another instance is already running; activating the existing window")
        _focus_existing_window(window_title)
        return None

    if handle is None:
        logger.warning("Single-instance mutex unavailable; continuing without guard")
        return SingleInstanceGuard()

    return SingleInstanceGuard(handle, _close_windows_handle)


def _create_windows_mutex(mutex_name: str):
    import ctypes

    ERROR_ALREADY_EXISTS = 183

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p

    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return None

    if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
        _close_windows_handle(handle)
        return _WINDOWS_MUTEX_ALREADY_EXISTS

    return handle


def _close_windows_handle(handle) -> None:
    import ctypes

    ctypes.windll.kernel32.CloseHandle(handle)


def _focus_existing_window(window_title: str) -> bool:
    """Try to show and focus the already-running main window."""
    import ctypes

    SW_SHOW = 5
    SW_RESTORE = 9

    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p

    hwnd = user32.FindWindowW(None, window_title)
    if not hwnd:
        logger.debug("No existing window found for title: %s", window_title)
        return False

    user32.ShowWindow(hwnd, SW_SHOW)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True
