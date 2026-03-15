"""Application orchestrator. Manages state, coordinates audio, transcription, and UI."""

import getpass
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from audio.capture import AudioCapture, AudioDevice, create_audio_capture
from audio.device_scanner import scan_all_devices
from audio.mixer import AudioMixer
from audio.recorder import WavRecorder
from config.constants import FIBERY_INSTANCE_URL
from config.keystore import get_key, keys_configured
from config.settings import Settings
from session import RecordingSession, SessionContext
from transcription.formatter import format_diarized_transcript

logger = logging.getLogger(__name__)


def _friendly_error(e: Exception) -> str:
    """Convert common exceptions to user-friendly messages."""
    msg = str(e)
    name = type(e).__name__
    if "ConnectionError" in name or "ConnectionRefused" in msg:
        return "Could not connect to the server. Check your internet connection."
    if "Timeout" in name or "timed out" in msg.lower():
        return "The request timed out. Try again."
    if "401" in msg or "Unauthorized" in msg:
        return "Authentication failed. Check your API keys in Settings."
    if "403" in msg or "Forbidden" in msg:
        return "Access denied. Check your API key permissions."
    if "429" in msg or "rate" in msg.lower():
        return "Rate limit exceeded. Try again later."
    if "500" in msg or "502" in msg or "503" in msg:
        return "Gemini temporarily unavailable. Try again later."
    if len(msg) > 200:
        return f"{name}: {msg[:150]}..."
    return msg


class FiberyTranscriptApp:
    """Main application orchestrator."""

    # Session states
    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    STATE_COMPLETED = "completed"

    def __init__(self, settings: Settings, data_dir: Path):
        self.settings = settings
        self.data_dir = data_dir
        self.state = self.STATE_IDLE
        self.window = None  # Set by main.py after window creation
        self.entity_panel = None  # Set by main.py after window creation

        # Audio
        self.audio_capture: AudioCapture = create_audio_capture()
        self._recorder: Optional[WavRecorder] = None
        self._mixer: Optional[AudioMixer] = None

        # Active recording session (created at start_recording / upload_and_transcribe)
        self._session: Optional[RecordingSession] = None

        # Cached Fibery validation (UI-level; may change while session is live)
        self._validated_entity = None
        self._fibery_client = None
        # Entity context for word boost / summary enrichment
        self._entity_context = None

        # Level update throttling
        self._last_mic_level: float = 0.0
        self._last_sys_level: float = 0.0

        # Device scanning
        self._scan_thread: Optional[threading.Thread] = None
        self._scan_stop_event = threading.Event()
        self._silence_counter_mic: int = 0   # consecutive silent ticks
        self._silence_counter_sys: int = 0
        self._selected_mic_index: Optional[int] = None
        self._selected_sys_index: Optional[int] = None
        self._is_shutting_down = False
        self._batch_thread: Optional[threading.Thread] = None

        # Lock to prevent concurrent stop_recording calls (sleep + silence race)
        self._stop_lock = threading.Lock()

        # Silence auto-stop tracking (active during STATE_RECORDING)
        self._recording_silence_start: Optional[float] = None
        self._auto_stop_countdown_active: bool = False
        self._last_silence_check: float = 0.0

        # Power monitor (set by main.py)
        self._power_monitor = None
        self._stopped_by_sleep: bool = False

        # Lock refresh tracking (refresh every 10 min during long recordings)
        self._last_lock_refresh: float = 0.0

        # Stashed results from previous recording segment (for continue-after-sleep)
        self._previous_batch_result: Optional[dict] = None
        self._previous_cleaned_transcript: Optional[str] = None

    @property
    def needs_close_confirmation(self) -> bool:
        """True when closing would lose work (recording or unsent transcript)."""
        if self.state in (self.STATE_RECORDING, self.STATE_PROCESSING):
            return True
        if self.state == self.STATE_COMPLETED and self._session:
            results = self._session.results
            if not self._session.context.entity:
                # Local-only: warn if transcript exists and user hasn't copied it
                return results.get_batch_result() is not None and not results.get_user_has_copied()
            else:
                # Entity linked at recording start: warn if transcript not sent yet
                return not results.get_transcript_sent()
        return False

    def save_settings(self) -> None:
        """Persist settings to disk."""
        settings_path = self.data_dir / "settings.json"
        self.settings.save(settings_path)
        logger.info("Settings saved to %s", settings_path)

    def reset_session(self) -> None:
        """Clear all session data and return to idle state.

        Called by JS when user clicks 'New Meeting'. Prevents stale
        transcript/summary from leaking into a subsequent session.
        """
        self.release_recording_lock()
        self._validated_entity = None
        self._entity_context = None
        self._session = None
        self._previous_batch_result = None
        self._previous_cleaned_transcript = None
        self.state = self.STATE_IDLE
        logger.info("Session reset")

    # --- Recording Lock ---

    def _get_display_name(self) -> str:
        return self.settings.display_name.strip() or getpass.getuser()

    def _build_lock_value(self) -> str:
        import socket
        from datetime import datetime, timezone
        name = self._get_display_name()
        host = socket.gethostname()
        return f"{name}@{host}|{datetime.now(timezone.utc).isoformat()}"

    def _parse_lock(self, raw: str):
        """Parse lock string into (display_name, hostname, timestamp_or_None).

        Supports v2 format: "Name@Host|ISO-timestamp"
        and legacy formats: "Name|ISO-timestamp" or "Name"
        """
        from datetime import datetime
        raw = raw.strip()
        timestamp = None
        if "|" in raw:
            identity, ts_str = raw.rsplit("|", 1)
            try:
                timestamp = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                pass
        else:
            identity = raw

        # Only split on '@' if this looks like a v2 lock (identity has host segment).
        # A legacy lock written before v2 may have an email address as the name;
        # we must not confuse the domain part for a hostname.
        # Heuristic: v2 identity ends with a plain hostname (no dots), legacy names
        # with '@' are email addresses and contain dots in the right-hand side.
        if "@" in identity:
            name_part, host_part = identity.rsplit("@", 1)
            # Accept as host only if it looks like a plain hostname (no dots)
            if "." not in host_part:
                name, host = name_part, host_part
            else:
                name, host = identity, ""  # treat as legacy email-style name
        else:
            name, host = identity, ""  # legacy or Phase 1 lock

        return name.strip(), host.strip(), timestamp

    def check_recording_lock(self) -> dict:
        if not self._validated_entity or not self._fibery_client:
            return {"locked": False}
        try:
            import socket
            from datetime import datetime, timezone, timedelta
            lock_val = self._fibery_client.get_recording_lock(self._validated_entity)
            if not lock_val:
                return {"locked": False}

            name, host, timestamp = self._parse_lock(lock_val)

            # Treat locks older than 30 minutes as stale
            if timestamp is not None:
                if datetime.now(timezone.utc) - timestamp > timedelta(minutes=30):
                    logger.info("Stale recording lock from %s@%s (set %s), ignoring", name, host, timestamp)
                    return {"locked": False}

            # Belongs to self if name AND hostname match
            my_name = self._get_display_name()
            my_host = socket.gethostname()
            if name == my_name and (not host or host == my_host):
                return {"locked": False}

            locked_by = f"{name}@{host}" if host else name
            return {"locked": True, "locked_by": locked_by}
        except Exception as e:
            logger.warning("Recording lock check failed: %s", e)
            return {"locked": False, "error": str(e)}

    def acquire_recording_lock(self) -> bool:
        if not self._validated_entity or not self._fibery_client:
            return False
        try:
            self._fibery_client.set_recording_lock(
                self._validated_entity, self._build_lock_value()
            )
            return True
        except Exception as e:
            logger.warning("Failed to acquire recording lock: %s", e)
            return False

    def release_recording_lock(self) -> None:
        if not self._validated_entity or not self._fibery_client:
            return
        try:
            import socket
            current = self._fibery_client.get_recording_lock(self._validated_entity)
            if current:
                name, host, _ = self._parse_lock(current)
                my_name = self._get_display_name()
                my_host = socket.gethostname()
                if name != my_name or (host and host != my_host):
                    logger.info("Lock belongs to %s@%s, not releasing", name, host)
                    return
            self._fibery_client.clear_recording_lock(self._validated_entity)
        except Exception as e:
            logger.warning("Failed to release recording lock: %s", e)

    def deselect_meeting(self) -> None:
        """Clear the currently linked Fibery entity without closing the panel."""
        if self.state == self.STATE_RECORDING and self._validated_entity:
            try:
                self.release_recording_lock()
            except Exception:
                logger.debug("Failed to release lock on deselect", exc_info=True)
        self._validated_entity = None
        self._entity_context = None
        logger.info("Meeting deselected")

    def _fetch_entity_context(self):
        """Fetch entity context from Fibery (or return cached). Thread-safe."""
        if self._entity_context is not None:
            return self._entity_context
        if not self._validated_entity or not self._fibery_client:
            return None
        try:
            from integrations.fibery_client import EntityContext
            ctx = self._fibery_client.get_entity_context(self._validated_entity)
            self._entity_context = ctx
            return ctx
        except Exception as e:
            logger.warning("Failed to fetch entity context: %s", e)
            return None

    # --- Level Monitoring ---

    def start_monitor(self, mic_index: Optional[int], loopback_index: Optional[int]) -> None:
        """Start audio level monitoring without recording."""
        if self.state in (self.STATE_RECORDING, self.STATE_PROCESSING):
            return

        # Stop any existing monitoring
        if self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()

        mic_device = self._find_device(mic_index, is_loopback=False) if mic_index is not None else None
        loopback_device = self._find_device(loopback_index, is_loopback=True) if loopback_index is not None else None

        if not mic_device and not loopback_device:
            return

        # Track selected devices for background scanner
        self._selected_mic_index = mic_index
        self._selected_sys_index = loopback_index
        self._silence_counter_mic = 0
        self._silence_counter_sys = 0

        self.audio_capture.start_capture(
            mic_device=mic_device,
            loopback_device=loopback_device,
            on_audio_chunk=lambda mic_pcm, sys_pcm: None,  # Discard audio data
            on_level_update=self._on_level_update,
        )
        logger.info("Level monitoring started (mic=%s, loopback=%s)",
                     mic_device and mic_device.name, loopback_device and loopback_device.name)

    def stop_monitor(self) -> None:
        """Stop audio level monitoring."""
        if self.state != self.STATE_RECORDING and self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()
            logger.info("Level monitoring stopped")

    # --- Device Scanning ---

    def scan_devices(self) -> dict:
        """One-shot scan of all devices for audio activity. Returns ScanReport as dict."""
        mic_devices = self.audio_capture.list_input_devices()
        loopback_devices = self.audio_capture.list_loopback_devices()

        report = scan_all_devices(mic_devices=mic_devices, loopback_devices=loopback_devices)
        logger.info(
            "Device scan: %d mics (%d active), %d loopbacks (%d active)",
            len(report.microphones),
            sum(1 for r in report.microphones if r.is_active),
            len(report.loopbacks),
            sum(1 for r in report.loopbacks if r.is_active),
        )
        return report.to_dict()

    def start_background_scanning(self) -> None:
        """Start periodic background device scanning."""
        if self._is_shutting_down:
            return
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._scan_stop_event.clear()
        self._scan_thread = threading.Thread(
            target=self._background_scan_loop, daemon=True,
        )
        self._scan_thread.start()
        logger.info("Background device scanning started")

    def stop_background_scanning(self) -> None:
        """Stop periodic background device scanning and wait for any in-progress scan."""
        self._scan_stop_event.set()
        if self._scan_thread:
            # Wait long enough for an in-progress scan to finish (~1.5s worst case)
            self._scan_thread.join(timeout=5.0)
            self._scan_thread = None
        logger.info("Background device scanning stopped")

    _SILENCE_THRESHOLD = 0.005   # RMS below this = "silent"
    _SILENCE_TICKS_NEEDED = 2    # consecutive silent ticks before scanning

    _RECORDING_SILENCE_DURATION = 180.0   # seconds of silence before countdown popup
    _AUTO_STOP_COUNTDOWN_SECONDS = 60     # countdown duration shown in popup

    def _background_scan_loop(self) -> None:
        """Background thread: only scans other devices when a selected source is silent."""
        while not self._scan_stop_event.is_set():
            if self._scan_stop_event.wait(timeout=5.0):
                break

            # Never scan during recording/processing or after window closes
            if (self._is_shutting_down
                    or not self.window
                    or self.state in (self.STATE_RECORDING, self.STATE_PROCESSING)):
                continue

            # Check which selected sources are silent
            mic_silent = (self._selected_mic_index is not None
                          and self._last_mic_level < self._SILENCE_THRESHOLD)
            sys_silent = (self._selected_sys_index is not None
                          and self._last_sys_level < self._SILENCE_THRESHOLD)

            if mic_silent:
                self._silence_counter_mic += 1
            else:
                self._silence_counter_mic = 0
            if sys_silent:
                self._silence_counter_sys += 1
            else:
                self._silence_counter_sys = 0

            # Only scan device types where the selected source has been silent
            # for multiple ticks (avoids scanning during brief pauses)
            scan_mics = self._silence_counter_mic >= self._SILENCE_TICKS_NEEDED
            scan_loopbacks = self._silence_counter_sys >= self._SILENCE_TICKS_NEEDED

            if not scan_mics and not scan_loopbacks:
                # Everything has audio - clear any warnings
                self._notify_js("window.onDeviceScanResults({\"microphones\":[],\"loopbacks\":[]})")
                continue

            try:
                mic_devices = (self.audio_capture.list_input_devices()
                               if scan_mics else [])
                loopback_devices = (self.audio_capture.list_loopback_devices()
                                    if scan_loopbacks else [])

                if self._scan_stop_event.is_set() or self.state == self.STATE_RECORDING:
                    continue

                report = scan_all_devices(
                    mic_devices=mic_devices,
                    loopback_devices=loopback_devices,
                    duration=0.3,
                    cancel=self._scan_stop_event,
                )
                data = json.dumps(report.to_dict())
                self._notify_js(f"window.onDeviceScanResults({data})")
            except Exception as e:
                logger.debug("Background scan error: %s", e)

    # --- Recording ---

    def start_recording(
        self,
        mic_index: Optional[int],
        loopback_index: Optional[int],
    ) -> None:
        """Start audio capture and WAV recording."""
        if self.state == self.STATE_RECORDING:
            raise RuntimeError("Already recording")

        # Pause background scanning during recording
        self.stop_background_scanning()

        # Stop monitoring if active
        if self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()

        self._recording_silence_start = None
        self._auto_stop_countdown_active = False
        self._stopped_by_sleep = False

        # Find devices by index
        mic_device = self._find_device(mic_index, is_loopback=False) if mic_index is not None else None
        loopback_device = self._find_device(loopback_index, is_loopback=True) if loopback_index is not None else None

        if mic_index is not None and mic_device is None:
            raise RuntimeError("Selected microphone is no longer available. Please refresh your audio devices.")
        if loopback_index is not None and loopback_device is None:
            raise RuntimeError("Selected speaker output is no longer available. Please refresh your audio devices.")

        # Start WAV recorder
        recordings_dir = Path(self.settings.recordings_dir) if self.settings.recordings_dir else self.data_dir / "recordings"
        self._recorder = WavRecorder(recordings_dir)
        self._recorder.start()

        # Set up audio mixer → feeds recorder
        self._mixer = AudioMixer(
            on_mixed_chunk=self._on_mixed_audio,
            has_mic=mic_device is not None,
            has_loopback=loopback_device is not None,
        )

        # Start audio capture
        self.audio_capture.start_capture(
            mic_device=mic_device,
            loopback_device=loopback_device,
            on_audio_chunk=self._on_audio_chunk,
            on_level_update=self._on_level_update,
        )

        # Create session — captures entity snapshot frozen at recording start
        self._session = RecordingSession(SessionContext(
            entity=self._validated_entity,
            fibery_client=self._fibery_client,
            entity_context=self._entity_context,
        ))

        self.state = self.STATE_RECORDING
        logger.info("Recording started (mic=%s, loopback=%s)",
                     mic_device and mic_device.name, loopback_device and loopback_device.name)

    def switch_sources(
        self,
        mic_index: Optional[int],
        loopback_index: Optional[int],
    ) -> None:
        """Switch audio sources while recording continues.

        The WAV recorder stays open. Only the capture layer and mixer
        are torn down and rebuilt with the new device configuration.
        """
        if self.state != self.STATE_RECORDING:
            raise RuntimeError("Can only switch sources while recording")

        mic_device = self._find_device(mic_index, is_loopback=False) if mic_index is not None else None
        loopback_device = self._find_device(loopback_index, is_loopback=True) if loopback_index is not None else None

        if not mic_device and not loopback_device:
            raise ValueError("At least one audio source is required")

        logger.info("Switching audio sources (mic=%s, loopback=%s)",
                    mic_device and mic_device.name, loopback_device and loopback_device.name)

        # Save old mixer for rollback
        old_mixer = self._mixer

        # Stop current capture
        self.audio_capture.stop_capture()

        # Flush remaining audio from current mixer into WAV
        if self._mixer:
            self._mixer.flush()

        # Build new mixer with updated source configuration
        self._mixer = AudioMixer(
            on_mixed_chunk=self._on_mixed_audio,
            has_mic=mic_device is not None,
            has_loopback=loopback_device is not None,
        )

        # Start new capture -- on failure, try to restart with old devices
        try:
            self.audio_capture.start_capture(
                mic_device=mic_device,
                loopback_device=loopback_device,
                on_audio_chunk=self._on_audio_chunk,
                on_level_update=self._on_level_update,
            )
        except Exception as e:
            logger.error("Failed to start new audio source: %s, attempting rollback", e)
            # Try to restore previous capture
            try:
                old_mic = self._find_device(self._selected_mic_index, False) if self._selected_mic_index is not None else None
                old_loop = self._find_device(self._selected_sys_index, True) if self._selected_sys_index is not None else None
                self._mixer = AudioMixer(
                    on_mixed_chunk=self._on_mixed_audio,
                    has_mic=old_mic is not None,
                    has_loopback=old_loop is not None,
                )
                self.audio_capture.start_capture(
                    mic_device=old_mic,
                    loopback_device=old_loop,
                    on_audio_chunk=self._on_audio_chunk,
                    on_level_update=self._on_level_update,
                )
                logger.info("Rolled back to previous audio sources")
            except Exception:
                logger.error("Rollback also failed, recording continues without audio")
            raise RuntimeError(f"Failed to switch to new audio source: {e}") from e

        # Update tracked device indices
        self._selected_mic_index = mic_index
        self._selected_sys_index = loopback_index
        logger.info("Audio sources switched successfully")

    def stop_recording(self) -> None:
        """Stop capture and start batch processing."""
        with self._stop_lock:
            if self.state != self.STATE_RECORDING:
                return
            self._stop_recording_inner()

    def _stop_recording_inner(self) -> None:
        """Inner stop logic (caller must hold _stop_lock)."""
        # Release recording lock
        self.release_recording_lock()

        # Stop audio capture
        self.audio_capture.stop_capture()

        # Flush mixer
        if self._mixer:
            self._mixer.flush()
            self._mixer = None

        # Stop WAV recorder (also finalizes parallel OGG compression)
        wav_path = None
        compressed_path = None
        if self._recorder:
            wav_path = self._recorder.stop()
            cp = self._recorder.compressed_path
            compressed_path = str(cp) if cp else None

        # Bake the file paths into the frozen session context
        if self._session and wav_path:
            self._session = RecordingSession(SessionContext(
                entity=self._session.context.entity,
                fibery_client=self._session.context.fibery_client,
                entity_context=self._session.context.entity_context,
                wav_path=str(wav_path),
                compressed_path=compressed_path or "",
                is_uploaded_file=False,
            ))

        self.state = self.STATE_PROCESSING
        logger.info("Recording stopped, starting batch processing")

        # Trigger batch processing in background
        session = self._session  # capture for background thread
        if wav_path and session:
            self._batch_thread = threading.Thread(
                target=self._run_batch_processing,
                args=(session,),
                daemon=True,
            )
            self._batch_thread.start()
        elif wav_path:
            # Fallback: no session (shouldn't happen), use paths directly
            self._batch_thread = threading.Thread(
                target=self._run_batch_processing,
                args=(RecordingSession(SessionContext(wav_path=str(wav_path), compressed_path=compressed_path or "")),),
                daemon=True,
            )
            self._batch_thread.start()
        else:
            # No audio recorded, just mark as completed
            self.state = self.STATE_COMPLETED
            self._notify_js("window.onProcessingComplete()")

    # --- File Upload (Browse & Transcribe) ---

    SUPPORTED_AUDIO_EXTENSIONS = {
        ".wav", ".ogg", ".flac", ".mp3", ".m4a", ".aac", ".wma", ".webm",
    }

    def _validate_audio_file(self, path: Path) -> dict:
        """Validate that a file is a readable audio file.

        Returns dict with format, duration_seconds, sample_rate, channels, size_bytes.
        Raises ValueError with a user-friendly message on failure.
        """
        suffix = path.suffix.lower()
        if suffix not in self.SUPPORTED_AUDIO_EXTENSIONS:
            raise ValueError(
                f"Unsupported format: {suffix}. "
                f"Supported: WAV, OGG, FLAC, MP3, M4A, AAC, WMA, WEBM"
            )

        if not path.exists():
            raise ValueError("File not found.")

        size_bytes = path.stat().st_size
        if size_bytes < 1024:
            raise ValueError("File is too small to be a valid audio file.")

        # Try soundfile first (handles WAV, OGG, FLAC natively)
        try:
            import soundfile as sf

            info = sf.info(str(path))
            if info.duration < 1.0:
                raise ValueError("Audio file is less than 1 second long.")
            return {
                "format": suffix.lstrip(".").upper(),
                "duration_seconds": info.duration,
                "sample_rate": info.samplerate,
                "channels": info.channels,
                "size_bytes": size_bytes,
            }
        except ValueError:
            raise
        except Exception:
            pass  # Fall through to pydub for MP3/M4A/AAC

        # Try pydub for formats soundfile can't handle
        try:
            from pydub import AudioSegment

            audio = AudioSegment.from_file(str(path))
            duration = len(audio) / 1000.0
            if duration < 1.0:
                raise ValueError("Audio file is less than 1 second long.")
            return {
                "format": suffix.lstrip(".").upper(),
                "duration_seconds": duration,
                "sample_rate": audio.frame_rate,
                "channels": audio.channels,
                "size_bytes": size_bytes,
            }
        except ValueError:
            raise
        except ImportError:
            raise ValueError(
                f"Cannot read {suffix} files. Install ffmpeg for MP3/M4A support."
            )
        except Exception as e:
            raise ValueError(f"Cannot read audio file: {e}")

    def upload_and_transcribe(self, file_path: str) -> None:
        """Validate an uploaded audio file and start batch processing.

        Transitions directly from idle/completed -> processing, skipping recording.
        """
        if self.state not in (self.STATE_IDLE, self.STATE_COMPLETED):
            raise RuntimeError(f"Cannot upload while in state: {self.state}")

        path = Path(file_path)
        self._validate_audio_file(path)  # raises on failure

        # Stop monitoring/scanning
        self.stop_background_scanning()
        if self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()

        # Determine wav_path and compressed_path for batch processing
        suffix = path.suffix.lower()
        wav_path = str(path)
        compressed_path = str(path) if suffix != ".wav" else ""

        # Create session for this upload
        self._session = RecordingSession(SessionContext(
            entity=self._validated_entity,
            fibery_client=self._fibery_client,
            entity_context=self._entity_context,
            wav_path=wav_path,
            compressed_path=compressed_path,
            is_uploaded_file=True,
        ))

        self.state = self.STATE_PROCESSING
        logger.info("Upload transcription started: %s", path.name)

        session = self._session
        self._batch_thread = threading.Thread(
            target=self._run_batch_processing,
            args=(session,),
            daemon=True,
        )
        self._batch_thread.start()

    def _find_device(self, index: int, is_loopback: bool) -> Optional[AudioDevice]:
        """Find a device by its index."""
        devices = (
            self.audio_capture.list_loopback_devices()
            if is_loopback
            else self.audio_capture.list_input_devices()
        )
        for dev in devices:
            if dev.index == index:
                return dev
        # Fallback: try all devices
        all_devices = self.audio_capture.list_input_devices() + self.audio_capture.list_loopback_devices()
        for dev in all_devices:
            if dev.index == index:
                return dev
        logger.warning("Device with index %d not found", index)
        return None

    # --- Audio Callbacks ---

    def _on_audio_chunk(self, mic_pcm: bytes, loopback_pcm: bytes) -> None:
        """Called by audio capture with raw PCM data."""
        if self._mixer:
            if mic_pcm:
                self._mixer.add_mic_audio(mic_pcm)
            if loopback_pcm:
                self._mixer.add_loopback_audio(loopback_pcm)

    def _on_mixed_audio(self, mixed_pcm: bytes) -> None:
        """Called by mixer with combined audio."""
        # Write to WAV file
        if self._recorder and self._recorder.is_recording:
            self._recorder.write_chunk(mixed_pcm)

    def _on_level_update(self, mic_level: float, sys_level: float) -> None:
        """Called by audio capture with RMS levels."""
        if mic_level >= 0:
            self._last_mic_level = mic_level
        if sys_level >= 0:
            self._last_sys_level = sys_level

        self._notify_js(
            f"window.updateAudioLevels({self._last_mic_level:.4f}, {self._last_sys_level:.4f})"
        )

        self._check_recording_silence()

    # --- Silence Auto-Stop ---

    def _check_recording_silence(self) -> None:
        """Check if both audio sources have been silent long enough to trigger auto-stop."""
        if self.state != self.STATE_RECORDING:
            return

        # Throttle to once per second
        now = time.monotonic()
        if now - self._last_silence_check < 1.0:
            return
        self._last_silence_check = now

        # Refresh lock every 10 minutes using the frozen session context
        if self._session and self._session.context.entity and self._session.context.fibery_client:
            if now - self._last_lock_refresh > 600:  # 10 minutes
                self._last_lock_refresh = now
                try:
                    self._session.context.fibery_client.set_recording_lock(
                        self._session.context.entity, self._build_lock_value()
                    )
                    logger.debug("Recording lock refreshed")
                except Exception:
                    logger.debug("Lock refresh failed", exc_info=True)

        both_silent = (self._last_mic_level < self._SILENCE_THRESHOLD
                       and self._last_sys_level < self._SILENCE_THRESHOLD)

        if both_silent:
            if self._recording_silence_start is None:
                self._recording_silence_start = now
            elapsed = now - self._recording_silence_start
            if (elapsed >= self._RECORDING_SILENCE_DURATION
                    and not self._auto_stop_countdown_active):
                self._auto_stop_countdown_active = True
                logger.info("Silence detected for %.0fs, showing countdown popup", elapsed)
                self._notify_js(
                    f"window.onSilenceCountdownStart({self._AUTO_STOP_COUNTDOWN_SECONDS})"
                )
        else:
            # Audio resumed
            self._recording_silence_start = None
            if self._auto_stop_countdown_active:
                self._auto_stop_countdown_active = False
                logger.info("Audio resumed, cancelling silence countdown")
                self._notify_js("window.onSilenceCountdownCancel()")

    def auto_stop_from_silence(self) -> None:
        """Called from JS when the silence countdown reaches zero."""
        self._auto_stop_countdown_active = False
        logger.info("Auto-stopping recording due to silence")
        self.stop_recording()
        self._notify_js("window.onAutoStopComplete()")

    def dismiss_silence_countdown(self) -> None:
        """Called from JS when user clicks 'Keep Recording'.

        Resets the silence timer so detection can re-trigger after another
        full silence duration (e.g. 3 minutes). Does NOT permanently disable.
        """
        self._auto_stop_countdown_active = False
        self._recording_silence_start = None
        logger.info("User dismissed silence countdown (will re-arm after next silence period)")

    # --- System Sleep / Wake ---

    def on_system_sleep(self) -> None:
        """Called by power monitor when the system is going to sleep."""
        logger.info("System sleep detected, state=%s", self.state)
        if self.state == self.STATE_RECORDING:
            self._stopped_by_sleep = True
            # Cancel any active silence countdown
            if self._auto_stop_countdown_active:
                self._auto_stop_countdown_active = False
                self._notify_js("window.onSilenceCountdownCancel()")
            self.stop_recording()
            self._notify_js("window.onSleepStop()")

    def on_system_wake(self) -> None:
        """Called by power monitor when the system wakes from sleep."""
        logger.info("System wake detected, stopped_by_sleep=%s, state=%s", self._stopped_by_sleep, self.state)
        if self._stopped_by_sleep:
            self._stopped_by_sleep = False
            self._notify_js("window.onSleepWakeNotification()")

        # Warn if processing was likely interrupted by sleep
        if self.state == self.STATE_PROCESSING:
            self._notify_js("window.onSleepDuringProcessing()")

        # Re-apply window icon (Win32 icon handles get invalidated after sleep)
        import threading
        from ui.window import reapply_win32_icon
        threading.Thread(target=reapply_win32_icon, daemon=True).start()

    def continue_recording(
        self,
        mic_index: Optional[int],
        loopback_index: Optional[int],
    ) -> None:
        """Continue recording after a sleep interruption.

        Stashes the current batch result and cleaned transcript so the next
        recording's processing can merge with them.
        """
        if self.state not in (self.STATE_PROCESSING, self.STATE_COMPLETED):
            raise RuntimeError("Nothing to continue from")

        # Stash current session results for merging into the next segment
        if self._session:
            prev_batch = self._session.results.get_batch_result()
            prev_transcript = self._session.results.get_cleaned_transcript()
            if prev_batch:
                self._previous_batch_result = prev_batch
            if prev_transcript:
                self._previous_cleaned_transcript = prev_transcript

        # Start fresh recording (creates a new session)
        self.start_recording(mic_index, loopback_index)
        logger.info("Continued recording after sleep (previous results stashed)")

    # --- Batch Processing ---

    def _run_batch_processing(self, session: "RecordingSession") -> None:
        """Run batch transcription with diarization (background thread).

        Receives the session snapshot created at recording stop — immune to
        entity/path changes that happen on the main thread after this is called.
        """
        ctx = session.context
        results = session.results
        wav_path = ctx.wav_path
        compressed_path = ctx.compressed_path or None

        try:
            from transcription.batch import transcribe_with_diarization

            def on_progress(msg):
                logger.info("Batch: %s", msg)
                self._notify_js(f"window.onProcessingProgress({json.dumps(msg)})")

            # Build word boost from frozen entity context
            word_boost = None
            entity_ctx = ctx.entity_context
            if not entity_ctx and ctx.entity and ctx.fibery_client:
                # Fallback: fetch if not captured at session start
                entity_ctx = self._fetch_entity_context()
            if entity_ctx:
                from integrations.context_builder import build_word_boost
                word_boost = build_word_boost(entity_ctx) or None

            result = transcribe_with_diarization(
                api_key=get_key("assemblyai_api_key"),
                audio_path=wav_path,
                on_progress=on_progress,
                compressed_path=compressed_path,
                word_boost=word_boost,
            )

            # Merge with previous recording segment if continuing after sleep
            if self._previous_batch_result:
                separator = {"speaker": "—", "text": "[Recording resumed after sleep]"}
                merged_utterances = (
                    self._previous_batch_result["utterances"]
                    + [separator]
                    + result["utterances"]
                )
                result["utterances"] = merged_utterances
                self._previous_batch_result = None
                logger.info("Merged with previous recording segment (%d total utterances)", len(merged_utterances))

            results.set_batch_result(result)

            # Update UI with raw diarized transcript immediately (fast feedback)
            utterances_json = json.dumps(result["utterances"])
            self._notify_js(f"window.setDiarizedTranscript({utterances_json})")

            # Run Gemini transcript cleanup (speaker identification, sentence fixes, sections)
            raw_text = format_diarized_transcript(result["utterances"])
            gemini_key = get_key("gemini_api_key")
            if gemini_key:
                try:
                    on_progress("Cleaning up transcript...")
                    from integrations.gemini_client import cleanup_transcript
                    from integrations.context_builder import build_summary_context

                    meeting_context = build_summary_context(entity_ctx) if entity_ctx else ""
                    cleaned = cleanup_transcript(
                        api_key=gemini_key,
                        transcript=raw_text,
                        language=result.get("language", "en"),
                        meeting_context=meeting_context,
                        company_context=self.settings.company_context,
                        model=self.settings.gemini_model_cleanup,
                    )
                    results.set_cleaned_transcript(cleaned)
                except Exception as e:
                    logger.warning("Transcript cleanup failed, using raw: %s", e)
                    results.set_cleaned_transcript(raw_text)
                    self._notify_js("window.onCleanupFailed()")
            else:
                results.set_cleaned_transcript(raw_text)

            # Merge with previous cleaned transcript if continuing after sleep
            cleaned_transcript = results.get_cleaned_transcript()
            if self._previous_cleaned_transcript:
                cleaned_transcript = (
                    self._previous_cleaned_transcript
                    + "\n\n---\n*[Recording resumed after sleep]*\n\n"
                    + cleaned_transcript
                )
                results.set_cleaned_transcript(cleaned_transcript)
                self._previous_cleaned_transcript = None
                logger.info("Merged cleaned transcript with previous segment")
            else:
                cleaned_transcript = results.get_cleaned_transcript()

            self._notify_js(f"window.setCleanedTranscript({json.dumps(cleaned_transcript)})")

            self.state = self.STATE_COMPLETED
            self._notify_js("window.onProcessingComplete()")

            # Auto-send transcript to Fibery using the frozen entity from session context
            if ctx.entity and ctx.fibery_client:
                threading.Thread(
                    target=self._auto_send_transcript,
                    args=(ctx.entity, ctx.fibery_client, session),
                    daemon=True,
                ).start()

            # Upload audio to Fibery if setting is "fibery"
            if self.settings.audio_storage == "fibery":
                self._upload_audio_to_fibery(wav_path, session)

            # Post-processing for uploaded (browsed) files:
            # Copy the compressed file to recordings_dir for local backup
            if ctx.is_uploaded_file and self.settings.save_recordings:
                self._copy_compressed_to_recordings(wav_path, compressed_path)

            # Clean up local recordings if user opted out of local storage
            if not ctx.is_uploaded_file and not self.settings.save_recordings:
                wav = Path(wav_path)
                ogg = wav.with_suffix('.ogg')
                for f in (wav, ogg):
                    if f.exists():
                        try:
                            f.unlink()
                            logger.info("Cleaned up local recording: %s", f.name)
                        except OSError as e:
                            logger.warning("Could not delete %s: %s", f.name, e)

            self.start_background_scanning()
            logger.info("Batch processing complete: %d utterances", len(result["utterances"]))

        except Exception as e:
            logger.error("Batch processing failed: %s", e)
            # Mark as completed even on failure
            self.state = self.STATE_COMPLETED
            self._notify_js(f"window.onBatchFailed({json.dumps({'message': _friendly_error(e), 'wav_path': wav_path or ''})})")
            self._notify_js(f"window.onError({json.dumps(_friendly_error(e))})")
            self.start_background_scanning()

    def _upload_audio_to_fibery(self, wav_path: str, session: "RecordingSession" = None) -> None:
        """Upload the audio recording to the linked Fibery entity's Files field."""
        # Use session context if available (prevents entity-swap bug)
        entity = session.context.entity if session else self._validated_entity
        client = session.context.fibery_client if session else self._fibery_client
        is_uploaded_file = session.context.is_uploaded_file if session else False

        if not entity or not client:
            return
        if not client.entity_supports_files(entity):
            logger.info(
                "Entity type %s does not support file attachments, skipping",
                entity.database,
            )
            return
        results = session.results if session else None
        if results and not results.try_start_audio_upload():
            logger.info("Audio upload already in-flight, skipping")
            return
        try:
            self._notify_js(
                'window.onProcessingProgress("Uploading audio to Fibery...")'
            )
            file_path = Path(wav_path)
            file_meta = client.upload_file(file_path)
            file_id = file_meta["fibery/id"]
            client.attach_file_to_entity(entity, file_id)
            if results:
                results.finish_audio_upload()
            logger.info("Audio file uploaded to Fibery: %s", file_path.name)
            self._notify_js("window.onAudioUploadedToFibery()")

            # Cleanup: for recorded files (not browsed), delete WAV, keep OGG
            if not is_uploaded_file:
                ogg_path = file_path.with_suffix(".ogg")
                if ogg_path.exists() and file_path.suffix.lower() == ".wav":
                    try:
                        file_path.unlink()
                        logger.info("Deleted local WAV after Fibery upload: %s", file_path.name)
                    except OSError as e:
                        logger.warning("Could not delete WAV: %s", e)

        except Exception as e:
            if results:
                results.finish_audio_upload()
            logger.error("Failed to upload audio to Fibery: %s", e)
            self._notify_js(
                f"window.onAudioUploadError({json.dumps(_friendly_error(e))})"
            )

    def _copy_compressed_to_recordings(self, wav_path: str, compressed_path: str = None) -> None:
        """Copy the compressed audio file to recordings_dir for browsed files."""
        import shutil

        recordings_dir = (
            Path(self.settings.recordings_dir)
            if self.settings.recordings_dir
            else self.data_dir / "recordings"
        )
        recordings_dir.mkdir(parents=True, exist_ok=True)

        source = Path(wav_path)
        # Check for compressed file created by batch.py (next to source)
        candidates = [
            Path(compressed_path) if compressed_path else None,
            source.with_suffix(".ogg"),
            source.with_suffix(".flac"),
        ]
        for candidate in candidates:
            if candidate and candidate.exists() and candidate != source:
                dest = recordings_dir / candidate.name
                if not dest.exists():
                    try:
                        shutil.copy2(str(candidate), str(dest))
                        logger.info("Copied compressed file to recordings: %s", dest.name)
                    except OSError as e:
                        logger.warning("Could not copy to recordings dir: %s", e)
                return

        # No compressed file found — copy the source itself if it's not WAV
        if source.suffix.lower() != ".wav":
            dest = recordings_dir / source.name
            if not dest.exists():
                try:
                    shutil.copy2(str(source), str(dest))
                    logger.info("Copied source file to recordings: %s", dest.name)
                except OSError as e:
                    logger.warning("Could not copy to recordings dir: %s", e)

    def _auto_send_pending_summary(self) -> None:
        """Send a cached generated summary to Fibery (background thread)."""
        result = self.send_pending_summary_to_fibery()
        if result.get("success"):
            self._notify_js("window.onPendingSummarySent()")
        else:
            self._notify_js(
                f"window.onPendingSummarySendError({json.dumps(result.get('error', 'Unknown error'))})"
            )

    def _auto_send_transcript(self, entity, fibery_client, session: "RecordingSession" = None) -> None:
        """Send the current transcript to the Fibery Transcript field (background thread).

        Args:
            entity: The FiberyEntity to send to.
            fibery_client: The FiberyClient to use.
            session: If provided, reads transcript from session.results and marks sent.
        """
        results = session.results if session else None

        if results:
            batch = results.get_batch_result()
            if not batch or not batch.get("utterances"):
                return
            transcript_text = results.get_cleaned_transcript() or format_diarized_transcript(batch["utterances"])
        else:
            # Fallback for post-recording entity link (no session results yet)
            if not self._session:
                return
            batch = self._session.results.get_batch_result()
            if not batch or not batch.get("utterances"):
                return
            transcript_text = (self._session.results.get_cleaned_transcript()
                                or format_diarized_transcript(batch["utterances"]))
            results = self._session.results

        if results and not results.try_start_transcript_send():
            logger.info("Transcript send already in-flight, skipping")
            return

        try:
            fibery_client.update_transcript_only(entity, transcript_text)
            if results:
                results.finish_transcript_send(success=True)
            self._notify_js("window.onTranscriptSentToFibery()")
            logger.info("Transcript auto-sent to Fibery")
        except Exception as exc:
            if results:
                results.finish_transcript_send(success=False)
            logger.error("Auto-send transcript to Fibery failed: %s", exc)
            self._notify_js(
                f"window.onTranscriptSendError({json.dumps(_friendly_error(exc))})"
            )

    # --- Fibery Integration ---


    # Meeting type definitions for entity creation
    MEETING_TYPES = {
        'internal': {'space': 'General', 'database': 'General/Internal Meeting'},
        'external': {'space': 'Network', 'database': 'Network/External Meeting'},
        'interview': {'space': 'Market', 'database': 'Market/Market Interview'},
    }

    def create_fibery_meeting(self, meeting_type: str, name: str) -> dict:
        """Create a new meeting entity in Fibery.

        Args:
            meeting_type: One of 'internal', 'external', 'interview'.
            name: Display name for the meeting.

        Returns:
            Dict with success, entity_name, database, space, url.
        """
        from datetime import date
        from integrations.fibery_client import FiberyClient

        if meeting_type not in self.MEETING_TYPES:
            return {'success': False, 'error': f'Unknown meeting type: {meeting_type}'}

        type_info = self.MEETING_TYPES[meeting_type]

        if not name:
            return {'success': False, 'error': 'Meeting name is required'}

        try:
            client = FiberyClient(
                api_token=get_key('fibery_api_token'),
                instance_url=FIBERY_INSTANCE_URL,
            )

            entity = client.create_entity(
                space=type_info['space'],
                database=type_info['database'],
                name=name,
                date=date.today().isoformat(),
            )

            entity_url = client.get_entity_url(entity)

            # Close old client before caching new one (prevents session leak)
            if self._fibery_client and self._fibery_client is not client:
                try:
                    self._fibery_client.close()
                except Exception:
                    pass
            self._validated_entity = entity
            self._fibery_client = client
            self._entity_context = None

            logger.info('Created Fibery meeting: %s (%s)', name, meeting_type)

            result = {
                'success': True,
                'entity_name': entity.entity_name,
                'database': entity.database,
                'space': entity.space,
                'url': entity_url,
            }

            # If recording is active, check for lock and include info
            if self.state == self.STATE_RECORDING:
                lock_info = self.check_recording_lock()
                if lock_info.get('locked'):
                    result['recording_lock'] = lock_info
                else:
                    self.acquire_recording_lock()

            return result
        except Exception as e:
            logger.error('Failed to create Fibery meeting: %s', e)
            return {'success': False, 'error': _friendly_error(e)}

    def validate_fibery_url(self, fibery_url: str) -> dict:
        """Validate a Fibery URL and return entity info."""
        from integrations.fibery_client import FiberyClient

        try:
            client = FiberyClient(
                api_token=get_key("fibery_api_token"),
                instance_url=FIBERY_INSTANCE_URL,
            )

            candidates = client.extract_url_candidates(fibery_url)

            if len(candidates) > 1:
                # Compound URL with two entity candidates — ask the user to pick
                resolved = []
                for candidate_url in candidates:
                    try:
                        entity = client.parse_url(candidate_url)
                        client.get_entity_uuid(entity)
                        name = client.get_entity_name(entity)
                        resolved.append({
                            "url": candidate_url,
                            "entity_name": name,
                            "database": entity.database,
                            "space": entity.space,
                        })
                    except Exception as exc:
                        logger.warning("Skipping candidate %s: %s", candidate_url, exc)

                if len(resolved) > 1:
                    return {"success": False, "needs_disambiguation": True, "candidates": resolved}
                # Only one resolved successfully — fall through with that URL
                if resolved:
                    fibery_url = resolved[0]["url"]

            elif len(candidates) == 1:
                fibery_url = candidates[0]

            entity = client.parse_url(fibery_url)
            client.get_entity_uuid(entity)
            name = client.get_entity_name(entity)
            logger.info("Validated Fibery entity: %s/%s - %s", entity.space, entity.database, name)

            # Close old client before caching new one (prevents session leak)
            if self._fibery_client and self._fibery_client is not client:
                try:
                    self._fibery_client.close()
                except Exception:
                    pass
            self._validated_entity = entity
            self._fibery_client = client
            self._entity_context = None

            # If transcript is already available (entity linked after recording), auto-send now
            session_batch = self._session.results.get_batch_result() if self._session else None
            if session_batch and session_batch.get("utterances"):
                threading.Thread(
                    target=self._auto_send_transcript,
                    args=(entity, client, self._session),
                    daemon=True,
                ).start()

            # If audio storage is Fibery and we have a recording, upload it now
            if self.settings.audio_storage == "fibery" and self._session and self._session.context.wav_path:
                threading.Thread(
                    target=self._upload_audio_to_fibery,
                    args=(self._session.context.wav_path, self._session),
                    daemon=True,
                ).start()

            # If a summary was already generated without a link, send it now
            pending_summary = bool(
                self._session.results.get_generated_summary() if self._session else None
            )
            if pending_summary:
                threading.Thread(target=self._auto_send_pending_summary, daemon=True).start()

            result = {
                "success": True,
                "entity_name": name,
                "database": entity.database,
                "space": entity.space,
                "pending_summary": pending_summary,
            }

            # If recording is active, check for lock and include info
            if self.state == self.STATE_RECORDING:
                lock_info = self.check_recording_lock()
                if lock_info.get("locked"):
                    result["recording_lock"] = lock_info
                else:
                    self.acquire_recording_lock()

            return result
        except Exception as e:
            logger.error("Fibery URL validation failed: %s", e)
            return {"success": False, "error": str(e)}

    def generate_summary(
        self,
        custom_prompt: str = "",
        summary_style: str = "normal",
    ) -> dict:
        """Generate an AI summary from the transcript without sending to Fibery.

        The summary is cached in self._generated_summary for later use.
        If a Fibery entity is already validated, also sends the summary there.
        """
        from integrations.gemini_client import summarize_transcript

        session_results = self._session.results if self._session else None
        batch = session_results.get_batch_result() if session_results else None
        if not (batch and batch.get("utterances")):
            return {"success": False, "error": "No transcript available"}

        try:
            # Use cleaned transcript if available, otherwise format raw utterances
            transcript_text = (session_results.get_cleaned_transcript() if session_results else None) or format_diarized_transcript(batch["utterances"])

            # Determine entity context for prompt (notes, interview vs meeting)
            # Use cached entity if available; otherwise use generic defaults
            notes = ""
            is_interview = False
            meeting_context = ""
            if self._validated_entity and self._fibery_client:
                try:
                    notes = self._fibery_client.get_entity_notes(self._validated_entity)
                    is_interview = self._validated_entity.database.lower() == "market interview"
                except Exception:
                    pass
                # Build dynamic meeting context from entity
                entity_ctx = self._fetch_entity_context()
                if entity_ctx:
                    from integrations.context_builder import build_summary_context
                    meeting_context = build_summary_context(entity_ctx)

            logger.info("Generating summary (style=%s, has_entity=%s)", summary_style, bool(self._validated_entity))

            summary = summarize_transcript(
                api_key=get_key("gemini_api_key"),
                transcript=transcript_text,
                notes=notes,
                is_interview=is_interview,
                custom_prompt=custom_prompt,
                summary_style=summary_style,
                model=self.settings.gemini_model,
                model_fallback=self.settings.gemini_model_fallback,
                company_context=self.settings.company_context,
                meeting_context=meeting_context,
            )

            if session_results:
                session_results.set_generated_summary(summary)
            logger.info("Summary generated (%d chars)", len(summary))

            # If entity already validated, send to Fibery immediately
            if self._validated_entity and self._fibery_client:
                if session_results and not session_results.try_start_summary_send():
                    logger.info("Summary send already in-flight, returning generated summary")
                    return {"success": True, "sent_to_fibery": False, "summary": summary}
                try:
                    self._fibery_client.update_summary_only(self._validated_entity, ai_summary=summary)
                    if session_results:
                        session_results.finish_summary_send(success=True)
                    logger.info("Summary sent to Fibery")
                    return {"success": True, "sent_to_fibery": True, "summary": summary}
                except Exception as e:
                    if session_results:
                        session_results.finish_summary_send(success=False)
                    logger.error("Failed to send summary to Fibery: %s", e)
                    return {"success": True, "sent_to_fibery": False, "fibery_error": _friendly_error(e), "summary": summary}

            return {"success": True, "sent_to_fibery": False, "summary": summary}

        except Exception as e:
            logger.error("Summary generation failed: %s", e)
            return {"success": False, "error": _friendly_error(e)}

    def send_pending_summary_to_fibery(self) -> dict:
        """Send a previously generated summary to the validated Fibery entity."""
        session_results = self._session.results if self._session else None
        summary = session_results.get_generated_summary() if session_results else None
        if not summary:
            return {"success": False, "error": "No summary available"}
        if not self._validated_entity or not self._fibery_client:
            return {"success": False, "error": "No Fibery entity validated"}
        if session_results and not session_results.try_start_summary_send():
            logger.info("Summary send already in-flight, skipping")
            return {"success": False, "error": "Summary send already in progress"}
        try:
            self._fibery_client.update_summary_only(self._validated_entity, ai_summary=summary)
            if session_results:
                session_results.finish_summary_send(success=True)
            logger.info("Pending summary sent to Fibery")
            return {"success": True}
        except Exception as e:
            if session_results:
                session_results.finish_summary_send(success=False)
            logger.error("Failed to send pending summary to Fibery: %s", e)
            return {"success": False, "error": _friendly_error(e)}

    def send_summary_to_fibery(
        self,
        fibery_url: str,
        custom_prompt: str = "",
        summary_style: str = "normal",
    ) -> dict:
        """Summarize transcript with Gemini and update the AI Summary field in Fibery."""

        from integrations.fibery_client import FiberyClient
        from integrations.gemini_client import summarize_transcript

        session_results = self._session.results if self._session else None
        batch = session_results.get_batch_result() if session_results else None
        if batch and batch.get("utterances"):
            transcript_text = (session_results.get_cleaned_transcript() or
                                format_diarized_transcript(batch["utterances"]))
        else:
            return {"success": False, "error": "No transcript available"}

        if session_results and not session_results.try_start_summary_send():
            logger.info("Summary send already in-flight, skipping")
            return {"success": False, "error": "Summary send already in progress"}

        try:
            if self._validated_entity:
                entity = self._validated_entity
                client = self._fibery_client
            else:
                client = FiberyClient(
                    api_token=get_key("fibery_api_token"),
                    instance_url=FIBERY_INSTANCE_URL,
                )
                entity = client.parse_url(fibery_url)
                client.get_entity_uuid(entity)

            logger.info("Summarizing for entity: %s/%s", entity.space, entity.database)

            notes = client.get_entity_notes(entity)
            is_interview = entity.database.lower() == "market interview"

            # Build dynamic meeting context
            meeting_context = ""
            entity_ctx = self._fetch_entity_context()
            if entity_ctx:
                from integrations.context_builder import build_summary_context
                meeting_context = build_summary_context(entity_ctx)

            summary = summarize_transcript(
                api_key=get_key("gemini_api_key"),
                transcript=transcript_text,
                notes=notes,
                is_interview=is_interview,
                custom_prompt=custom_prompt,
                summary_style=summary_style,
                model=self.settings.gemini_model,
                model_fallback=self.settings.gemini_model_fallback,
                company_context=self.settings.company_context,
                meeting_context=meeting_context,
            )

            if self._session:
                self._session.results.set_generated_summary(summary)
            client.update_summary_only(entity, ai_summary=summary)
            if self._session:
                self._session.results.finish_summary_send(success=True)
            logger.info("AI Summary updated in Fibery")
            return {"success": True}

        except Exception as e:
            if self._session:
                self._session.results.finish_summary_send(success=False)
            logger.error("Fibery summarize workflow failed: %s", e)
            return {"success": False, "error": _friendly_error(e)}

    # --- UI Communication ---

    def _emergency_stop_recording(self) -> None:
        """Stop recording and save files without triggering batch processing.

        Used during shutdown to ensure WAV/OGG files are properly finalized
        (headers written, files closed) even though we won't transcribe.
        """
        with self._stop_lock:
            if self.state != self.STATE_RECORDING:
                return
            logger.info("Emergency stop: saving recording files before shutdown")
            self.audio_capture.stop_capture()
            if self._mixer:
                self._mixer.flush()
                self._mixer = None
            if self._recorder:
                self._recorder.stop()
            self.state = self.STATE_IDLE

    def begin_shutdown(self) -> None:
        """Mark app as shutting down and stop all background activity."""
        if self._is_shutting_down:
            return
        self._emergency_stop_recording()
        self.release_recording_lock()
        # Wait briefly for batch processing to finish (avoid losing transcript)
        if self._batch_thread and self._batch_thread.is_alive():
            logger.info("Waiting for batch processing to complete...")
            self._batch_thread.join(timeout=5.0)
            if self._batch_thread.is_alive():
                logger.warning("Batch processing still running, forcing shutdown")
        self._is_shutting_down = True
        self._scan_stop_event.set()
        if self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()
        self.stop_background_scanning()
        if self._power_monitor:
            self._power_monitor.stop()
            self._power_monitor = None
        if self._fibery_client:
            try:
                self._fibery_client.close()
            except Exception as e:
                logger.debug("Error closing Fibery client: %s", e)
        self.window = None

    def _notify_js(self, js_code: str) -> None:
        """Execute JavaScript in the webview window."""
        if self._is_shutting_down or not self.window:
            return
        try:
            self.window.evaluate_js(js_code)
        except Exception as e:
            logger.debug("JS notify failed: %s", e)
