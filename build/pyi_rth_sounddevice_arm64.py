"""PyInstaller runtime hook: fix sounddevice PortAudio DLL on ARM64 Windows.

When x64 Python runs on ARM64 Windows (under emulation), platform.machine()
returns 'ARM64' but the bundled _sounddevice_data only has x64 DLLs.
Sounddevice looks for libportaudioarm64.dll which doesn't exist.

Fix: copy the x64 DLL with the ARM64 name so sounddevice can find it.
The x64 DLL works fine under Windows' x64 emulation layer.
"""

import os
import platform
import shutil
import sys

if platform.system() == 'Windows' and platform.machine().lower() in ('arm64', 'aarch64'):
    _base = os.path.join(sys._MEIPASS, '_sounddevice_data', 'portaudio-binaries')
    _arm64_dll = os.path.join(_base, 'libportaudioarm64.dll')
    _x64_dll = os.path.join(_base, 'libportaudio64bit.dll')
    if not os.path.exists(_arm64_dll) and os.path.exists(_x64_dll):
        shutil.copy2(_x64_dll, _arm64_dll)
