"""Register/unregister auto-start on boot for each platform."""

import logging
import sys
from pathlib import Path

from config.constants import (
    APP_AUTOSTART_REG_VALUE,
    APP_LEGACY_AUTOSTART_REG_VALUES,
    APP_NAME,
)
from utils.platform_utils import get_platform

logger = logging.getLogger(__name__)


def set_autostart(enabled: bool) -> bool:
    """Enable or disable auto-start on boot. Returns True on success."""
    plat = get_platform()
    try:
        if plat == "windows":
            return _autostart_windows(enabled)
        elif plat == "macos":
            return _autostart_macos(enabled)
        else:
            return _autostart_linux(enabled)
    except Exception as e:
        logger.error("Failed to %s autostart: %s", "enable" if enabled else "disable", e)
        return False


def _autostart_windows(enabled: bool) -> bool:
    """Windows: use registry Run key."""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    exe_path = sys.executable

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        try:
            _remove_windows_autostart_values(winreg, key)
            if enabled:
                winreg.SetValueEx(
                    key,
                    APP_AUTOSTART_REG_VALUE,
                    0,
                    winreg.REG_SZ,
                    f'"{exe_path}"',
                )
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as e:
        logger.error("Registry autostart error: %s", e)
        return False


def _windows_autostart_value_names() -> tuple[str, ...]:
    names = [APP_AUTOSTART_REG_VALUE, *APP_LEGACY_AUTOSTART_REG_VALUES]
    return tuple(dict.fromkeys(names))


def _remove_windows_autostart_values(winreg, key) -> None:
    for name in _windows_autostart_value_names():
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def _autostart_macos(enabled: bool) -> bool:
    """macOS: use LaunchAgents plist."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"com.fiberytranscript.app.plist"

    if enabled:
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.fiberytranscript.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
        plist_path.write_text(plist_content)
        return True
    else:
        if plist_path.exists():
            plist_path.unlink()
        return True


def _autostart_linux(enabled: bool) -> bool:
    """Linux: use XDG autostart .desktop file."""
    autostart_dir = Path.home() / ".config" / "autostart"
    desktop_path = autostart_dir / f"{APP_NAME.lower()}.desktop"

    if enabled:
        autostart_dir.mkdir(parents=True, exist_ok=True)
        desktop_content = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={sys.executable}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
"""
        desktop_path.write_text(desktop_content)
        return True
    else:
        if desktop_path.exists():
            desktop_path.unlink()
        return True
