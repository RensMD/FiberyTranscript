# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Fibery Transcript.
Cross-platform: detects OS and adjusts config accordingly.

Usage:
    pyinstaller build/fibery_transcript.spec
"""

import sys
import os

block_cipher = None

# Paths
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

# Platform detection
IS_WINDOWS = sys.platform == 'win32'
IS_MACOS = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')

# Hidden imports (platform-dependent)
hidden_imports = [
    # pywebview backends
    'webview',
    # Audio
    'sounddevice',
    'soundfile',
    '_sounddevice_data',
    # Transcription
    'assemblyai',
    # AI
    'google.genai',
    'google.genai.types',
    'google.auth',
    # Key storage
    'keyring',
    'keyring.backends',
    # System tray
    'pystray',
    'PIL',
    'PIL.Image',
    # Misc
    'scipy.signal',
]

if IS_WINDOWS:
    hidden_imports += [
        'pyaudiowpatch',
        'clr_loader',
        'pythonnet',
        'webview.platforms.winforms',
    ]
elif IS_MACOS:
    hidden_imports += [
        'webview.platforms.cocoa',
        'objc',
    ]
else:
    hidden_imports += [
        'webview.platforms.gtk',
        'gi',
    ]

# Data files
datas = [
    (os.path.join(PROJECT_ROOT, 'ui', 'static'), os.path.join('ui', 'static')),
]

# Exclude config/secrets.py from the bundle — keys come from keyring/env
excludes = [
    'tkinter',
    'test',
    'tests',
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'main.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPECPATH, 'pyi_rth_sounddevice_arm64.py')],
    excludes=excludes,
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FiberyTranscript',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=os.path.join(PROJECT_ROOT, 'ui', 'static', 'icon.ico') if IS_WINDOWS else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FiberyTranscript',
)

# macOS .app bundle
if IS_MACOS:
    app = BUNDLE(
        coll,
        name='FiberyTranscript.app',
        icon=os.path.join(PROJECT_ROOT, 'ui', 'static', 'icon.icns')
            if os.path.exists(os.path.join(PROJECT_ROOT, 'ui', 'static', 'icon.icns'))
            else None,
        bundle_identifier='com.fiberytranscript.app',
        info_plist={
            'NSMicrophoneUsageDescription': 'Fibery Transcript needs microphone access to record meetings.',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleName': 'Fibery Transcript',
            'LSMinimumSystemVersion': '11.0',
        },
    )
