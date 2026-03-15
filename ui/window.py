"""pywebview window management."""

import logging
import webview

from utils.platform_utils import get_resource_path

logger = logging.getLogger(__name__)

# Module-level cache for icon path (used for reapply after sleep/wake)
_cached_ico_path: str = ""


def create_window(api_bridge, title: str = "FiberyTranscript", confirm_close: bool = False) -> webview.Window:
    """Create and return the main pywebview window.

    Args:
        api_bridge: The ApiBridge instance to expose to JavaScript.
        title: Window title.
        confirm_close: If True, show a native confirmation dialog before closing.

    Returns:
        The created webview.Window object.
    """
    html_path = get_resource_path("ui/static/index.html")
    logger.info("Loading UI from: %s", html_path)

    window = webview.create_window(
        title=title,
        url=str(html_path),
        js_api=api_bridge,
        width=500,
        height=800,
        min_size=(400, 700),
        text_select=True,
        confirm_close=confirm_close,
    )

    return window


def _build_ico() -> str:
    """Convert icon.png to icon.ico (always regenerate). Returns ICO path, falls back to PNG."""
    png_path = get_resource_path("ui/static/icon.png")
    ico_path = get_resource_path("ui/static/icon.ico")
    try:
        from PIL import Image
        img = Image.open(str(png_path)).convert("RGBA")
        img.save(
            str(ico_path),
            format="ICO",
            sizes=[
                (16, 16),
                (24, 24),
                (32, 32),
                (40, 40),
                (48, 48),
                (64, 64),
                (128, 128),
                (256, 256),
            ],
        )
        return str(ico_path)
    except Exception as e:
        logger.warning("Could not create icon.ico: %s", e)
        return str(png_path)


def _apply_win32_icon(ico_path: str) -> None:
    """Set taskbar icon via Win32 API. Runs in background thread after webview starts."""
    import ctypes
    import ctypes.wintypes as wt
    import os
    import time

    time.sleep(1.0)  # Wait for window to finish creating

    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    SM_CXICON = 11
    SM_CYICON = 12
    SM_CXSMICON = 49
    SM_CYSMICON = 50

    user32 = ctypes.windll.user32

    # Set proper 64-bit-safe function signatures
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SendMessageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wt.DWORD),
    ]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    small_w = user32.GetSystemMetrics(SM_CXSMICON) or 16
    small_h = user32.GetSystemMetrics(SM_CYSMICON) or 16
    big_w = user32.GetSystemMetrics(SM_CXICON) or 32
    big_h = user32.GetSystemMetrics(SM_CYICON) or 32

    hicon_small = user32.LoadImageW(
        None, ico_path, IMAGE_ICON, small_w, small_h, LR_LOADFROMFILE
    )
    hicon_big = user32.LoadImageW(
        None, ico_path, IMAGE_ICON, big_w, big_h, LR_LOADFROMFILE
    )

    if not hicon_small and not hicon_big:
        logger.warning("LoadImageW failed for: %s", ico_path)
        return
    if not hicon_small:
        hicon_small = hicon_big
    if not hicon_big:
        hicon_big = hicon_small

    current_pid = os.getpid()
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    hwnds = []

    def enum_cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == current_pid and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(enum_cb), None)
    logger.info("Applying Win32 icon to %d window(s)", len(hwnds))
    for hwnd in hwnds:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)


def reapply_win32_icon() -> None:
    """Re-apply the window icon after sleep/wake. Call from a background thread."""
    import sys
    if not _cached_ico_path or sys.platform != "win32":
        return
    import time
    time.sleep(0.5)
    _apply_win32_icon(_cached_ico_path)


def start_webview(debug: bool = False) -> None:
    """Start the pywebview event loop. Blocks until window is closed."""
    import sys
    global _cached_ico_path

    from utils.platform_utils import get_data_dir
    storage_path = str(get_data_dir() / "webview_storage")

    ico_path = _build_ico()
    _cached_ico_path = ico_path
    if sys.platform == "win32":
        webview.start(
            func=_apply_win32_icon,
            args=(ico_path,),
            debug=debug,
            private_mode=False,
            storage_path=storage_path,
        )
    else:
        webview.start(
            icon=ico_path,
            debug=debug,
            private_mode=False,
            storage_path=storage_path,
        )
