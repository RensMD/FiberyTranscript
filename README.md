# Fibery Transcript

Desktop app for recording meetings, transcribing audio with AssemblyAI (speaker diarization), generating AI summaries with Gemini, and pushing results to Fibery entities.

---

## Requirements

- Python 3.11+
- API keys: AssemblyAI, Google Gemini, Fibery token

```bash
pip install -r requirements.txt
```

---

## Running (dev)

```bash
.venv\Scripts\python.exe main.py
```

On first launch, the app shows a setup screen to enter API keys. These are stored in the system keyring (Windows Credential Locker / macOS Keychain / Linux Secret Service) — never in plain text on disk.

---

## Private config (recommended before publishing)

- Copy `config/private_context.example.py` to `config/private_context.py` for company-specific Fibery URL and name-disambiguation context.
- Copy `config/secrets.example.py` to `config/secrets.py` only if you need local dev fallback keys.
- Both `config/private_context.py` and `config/secrets.py` are ignored by git.

---

## Building

### Windows

**Prerequisite:** [NSIS](https://nsis.sourceforge.io/) (`makensis` in PATH)

```bash
python build/build.py
```

Output: `dist/FiberyTranscript-Setup.exe`

- Installs to `%LOCALAPPDATA%\FiberyTranscript\` (no admin required)
- Start Menu shortcut created
- Uninstaller registered in Add/Remove Programs
- Users will see a SmartScreen warning (no code signing) — click "More info" → "Run anyway"

---

### macOS

```bash
python build/build.py
```

Output: `dist/FiberyTranscript.dmg`

- Drag-to-Applications install
- First launch: right-click → Open (Gatekeeper blocks unsigned apps on double-click; only needed once)
- Microphone permission dialog appears automatically on first recording
- System audio capture requires [BlackHole](https://existential.audio/blackhole/) to be installed separately; the app works mic-only without it

---

### Linux (Ubuntu / Debian)

```bash
python build/build.py
```

Output: `dist/FiberyTranscript.AppImage` (if [`appimagetool`](https://github.com/AppImage/AppImageKit/releases) is in PATH), otherwise the AppDir is left at `dist/FiberyTranscript.AppDir/` and can be run directly.

```bash
chmod +x FiberyTranscript.AppImage
./FiberyTranscript.AppImage
```

System audio capture on Linux requires a PulseAudio/PipeWire loopback sink.

---

### Clean rebuild

```bash
python build/build.py --clean
```

Deletes `dist/` and `build/output/` before rebuilding.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| Theme | Dark | Light or dark UI |
| Save recordings | On | Keep WAV/OGG files after transcription |
| Recordings folder | `~/AppData/.../recordings` | Where audio files are saved |
| Audio-supported transcript cleanup | Off | Use Gemini with the audio file to improve the AssemblyAI transcript |
| Gemini Interview Model | `gemini-3.1-pro-preview` | Model used for interview summaries |
| Gemini Meeting Model | `gemini-3-flash-preview` | Model used for meeting summaries |

---

## Project structure

```
main.py                   Entry point
app.py                    Core application logic
ui/
  window.py               pywebview window setup
  api_bridge.py           Python ↔ JavaScript API
  static/
    index.html
    css/styles.css
    js/app.js             Main UI logic
    js/settings.js        Settings panel
    js/transcript.js      Transcript display
    js/audio-viz.js       Level meter visualizer
audio/
  capture_windows.py      WASAPI loopback + sounddevice capture
  recorder.py             WAV + parallel OGG recording
  level_monitor.py        RMS level calculation
transcription/
  batch.py                AssemblyAI upload + diarization
integrations/
  fibery_client.py        Fibery API (entity fetch, transcript/summary update)
  gemini_client.py        Gemini summarization
config/
  settings.py             Settings dataclass + JSON persistence
  keystore.py             Secure API key storage (keyring)
  constants.py            Audio constants + AI prompts
build/
  fibery_transcript.spec  PyInstaller spec (cross-platform)
  installer.nsi           Windows NSIS installer
  build.py                Build script
  macos/entitlements.plist
  linux/FiberyTranscript.desktop
```
