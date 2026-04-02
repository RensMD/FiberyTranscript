#!/usr/bin/env python3
"""
Build script for Fibery Transcript.
Automates PyInstaller build and platform-specific post-processing.

Usage:
    python build/build.py          # Build for current platform
    python build/build.py --clean  # Clean previous build artifacts first
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"
SPEC_FILE = BUILD_DIR / "fibery_transcript.spec"
INDEX_HTML = PROJECT_ROOT / "ui" / "static" / "index.html"

VERSIONED_ASSETS = (
    "icon.ico",
    "icon.png",
    "icon.svg",
    "css/styles.css",
    "js/audio-viz.js",
    "js/transcript.js",
    "js/settings.js",
    "js/app.js",
)

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def clean():
    """Remove previous build artifacts."""
    for d in [DIST_DIR, PROJECT_ROOT / "build" / "output"]:
        if d.exists():
            print(f"Cleaning {d}...")
            shutil.rmtree(d)


def check_dependencies():
    """Verify build dependencies are installed."""
    try:
        import PyInstaller
        print(f"PyInstaller {PyInstaller.__version__} found")
    except ImportError:
        print("ERROR: PyInstaller not installed. Run: pip install pyinstaller>=6.0")
        sys.exit(1)

    if IS_WINDOWS:
        try:
            import pyaudiowpatch
            print("PyAudioWPatch found")
        except ImportError:
            print("WARNING: PyAudioWPatch not installed (needed for Windows loopback)")


def build_pyinstaller():
    """Run PyInstaller with the spec file."""
    print(f"\nBuilding with PyInstaller (platform: {sys.platform})...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR / "output"),
        str(SPEC_FILE),
    ]
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print("ERROR: PyInstaller build failed")
        sys.exit(1)
    print("PyInstaller build complete!")


def _get_app_version() -> str:
    """Read APP_VERSION from config/constants.py."""
    constants_path = PROJECT_ROOT / "config" / "constants.py"
    with open(constants_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("APP_VERSION"):
                # APP_VERSION = "1.3.0"
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def _bump_patch_version() -> str:
    """Increment the patch version in config/constants.py and return the new version."""
    constants_path = PROJECT_ROOT / "config" / "constants.py"
    content = constants_path.read_text(encoding="utf-8")

    old_version = _get_app_version()
    parts = old_version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    new_version = ".".join(parts)

    content = content.replace(
        f'APP_VERSION = "{old_version}"',
        f'APP_VERSION = "{new_version}"',
    )
    constants_path.write_text(content, encoding="utf-8")
    print(f"Version bumped: {old_version} -> {new_version}")
    return new_version


def _sync_versioned_asset_urls(html: str, version: str) -> str:
    updated = html
    for asset in VERSIONED_ASSETS:
        pattern = rf'((?:href|src)=["\']){re.escape(asset)}(?:\?v=[^"\']*)?(["\'])'
        replacement = rf'\1{asset}?v={version}\2'
        updated = re.sub(pattern, replacement, updated)
    return updated


def sync_index_asset_versions(version: str) -> bool:
    """Ensure versioned UI asset URLs in index.html match APP_VERSION."""
    content = INDEX_HTML.read_text(encoding="utf-8")
    updated = _sync_versioned_asset_urls(content, version)
    if updated == content:
        return False
    INDEX_HTML.write_text(updated, encoding="utf-8")
    print(f"Synchronized index asset version tokens to {version}")
    return True


def post_build_windows():
    """Windows: build NSIS installer."""
    nsi_file = BUILD_DIR / "installer.nsi"
    if not nsi_file.exists():
        print("WARNING: installer.nsi not found, skipping NSIS build")
        return

    # Check if NSIS is available
    makensis = shutil.which("makensis")
    if not makensis:
        print("WARNING: NSIS (makensis) not found in PATH.")
        print("  Install NSIS from https://nsis.sourceforge.io/")
        print("  Or manually run: makensis build/installer.nsi")
        return

    version = _get_app_version()
    print(f"\nBuilding NSIS installer (v{version})...")
    result = subprocess.run(
        [makensis, f"-DVERSION={version}", str(nsi_file)],
        cwd=str(BUILD_DIR),
    )
    if result.returncode != 0:
        print("ERROR: NSIS build failed")
    else:
        installer = DIST_DIR / f"FiberyTranscript-{version}-Setup.exe"
        if installer.exists():
            size_mb = installer.stat().st_size / (1024 * 1024)
            print(f"Installer created: {installer} ({size_mb:.1f} MB)")


def post_build_macos():
    """macOS: generate .icns and create .dmg."""
    icon_png = PROJECT_ROOT / "ui" / "static" / "icon.png"
    icon_icns = PROJECT_ROOT / "ui" / "static" / "icon.icns"

    # Generate .icns if not exists
    if not icon_icns.exists() and icon_png.exists():
        print("\nGenerating icon.icns...")
        iconset = Path("/tmp/icon.iconset")
        iconset.mkdir(exist_ok=True)
        sizes = [16, 32, 64, 128, 256, 512]
        for s in sizes:
            subprocess.run(["sips", "-z", str(s), str(s), str(icon_png),
                          "--out", str(iconset / f"icon_{s}x{s}.png")],
                         capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icon_icns)],
                      capture_output=True)
        if icon_icns.exists():
            print(f"Created {icon_icns}")

    # Create .dmg
    app_path = DIST_DIR / "FiberyTranscript.app"
    if app_path.exists():
        print("\nCreating DMG...")
        dmg_path = DIST_DIR / "FiberyTranscript.dmg"
        # Simple DMG creation using hdiutil
        subprocess.run([
            "hdiutil", "create",
            "-volname", "Fibery Transcript",
            "-srcfolder", str(app_path),
            "-ov",
            "-format", "UDZO",
            str(dmg_path),
        ])
        if dmg_path.exists():
            size_mb = dmg_path.stat().st_size / (1024 * 1024)
            print(f"DMG created: {dmg_path} ({size_mb:.1f} MB)")
    else:
        print(f"WARNING: {app_path} not found, skipping DMG creation")


def post_build_linux():
    """Linux: create AppImage."""
    app_dir = DIST_DIR / "FiberyTranscript.AppDir"
    pyinstaller_dir = DIST_DIR / "FiberyTranscript"

    if not pyinstaller_dir.exists():
        print("WARNING: PyInstaller output not found, skipping AppImage")
        return

    print("\nCreating AppImage structure...")

    # Build AppDir
    usr_bin = app_dir / "usr" / "bin"
    usr_bin.mkdir(parents=True, exist_ok=True)

    # Copy PyInstaller output
    shutil.copytree(pyinstaller_dir, usr_bin, dirs_exist_ok=True)

    # Copy desktop file and icon
    shutil.copy(BUILD_DIR / "linux" / "FiberyTranscript.desktop", app_dir)
    shutil.copy(PROJECT_ROOT / "ui" / "static" / "icon.png", app_dir)

    # Create AppRun symlink
    apprun = app_dir / "AppRun"
    if apprun.exists():
        apprun.unlink()
    os.symlink("usr/bin/FiberyTranscript", str(apprun))

    # Build AppImage
    appimagetool = shutil.which("appimagetool")
    if not appimagetool:
        print("WARNING: appimagetool not found. Download from:")
        print("  https://github.com/AppImage/AppImageKit/releases")
        print(f"  AppDir ready at: {app_dir}")
        return

    appimage_path = DIST_DIR / "FiberyTranscript.AppImage"
    result = subprocess.run(
        [appimagetool, str(app_dir), str(appimage_path)],
        env={**os.environ, "ARCH": "x86_64"},
    )
    if result.returncode == 0 and appimage_path.exists():
        size_mb = appimage_path.stat().st_size / (1024 * 1024)
        print(f"AppImage created: {appimage_path} ({size_mb:.1f} MB)")


def main():
    if "--clean" in sys.argv:
        clean()

    if "--no-bump" not in sys.argv:
        _bump_patch_version()

    sync_index_asset_versions(_get_app_version())

    check_dependencies()
    build_pyinstaller()

    if IS_WINDOWS:
        post_build_windows()
    elif IS_MACOS:
        post_build_macos()
    elif IS_LINUX:
        post_build_linux()

    print("\nBuild complete!")
    print(f"Output: {DIST_DIR}")


if __name__ == "__main__":
    main()
