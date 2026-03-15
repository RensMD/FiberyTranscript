"""Platform detection, data directories, and resource path resolution."""

import os
import sys
import platform
from pathlib import Path

from config.constants import APP_NAME


def get_platform() -> str:
    """Return 'windows', 'macos', or 'linux'."""
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    elif system == "darwin":
        return "macos"
    else:
        return "linux"


def get_data_dir() -> Path:
    """Get the platform-appropriate app data directory."""
    plat = get_platform()
    if plat == "windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif plat == "macos":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data_dir = base / APP_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_resource_path(relative_path: str) -> Path:
    """Get absolute path to a bundled resource. Works for dev and PyInstaller."""
    if hasattr(sys, "_MEIPASS"):
        # Running as PyInstaller bundle
        return Path(sys._MEIPASS) / relative_path
    # Running in development
    return Path(__file__).parent.parent / relative_path


def is_frozen() -> bool:
    """Return True if running as a PyInstaller bundle."""
    return hasattr(sys, "_MEIPASS")
