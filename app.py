"""Application orchestrator. Manages state, coordinates audio, transcription, and UI."""

import getpass
import json
import logging
import threading
import time
from dataclasses import dataclass, field
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


@dataclass
class RecordingCheckpoint:
    """A point-in-time marker during recording for the decision popup.

    Created on sleep events (laptop close) or silence detection milestones.
    The user can choose to discard all audio after a given checkpoint.
    """
    type: str            # "sleep" or "silence"
    meeting_secs: float  # meeting duration at checkpoint (excluding sleep/silence gaps)
    segment_index: int   # len(_recording_segments) when created; keep segments[0:segment_index]


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
    if "not found" in msg.lower() or "Entity not found" in msg:
        return "Meeting not found in Fibery. It may have been deleted."
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

        # Level update throttling — limit evaluate_js calls to ~5/sec
        self._last_mic_level: float = 0.0
        self._last_sys_level: float = 0.0
        self._last_level_push: float = 0.0
        self._LEVEL_PUSH_INTERVAL: float = 0.2  # seconds between JS level pushes

        # Device scanning
        self._scan_thread: Optional[threading.Thread] = None
        self._scan_stop_event = threading.Event()
        self._silence_counter_mic: int = 0   # consecutive silent ticks
        self._silence_counter_sys: int = 0
        self._selected_mic_index: Optional[int] = None
        self._selected_sys_index: Optional[int] = None
        self._is_shutting_down = False
        self._tray_quit_requested = False
        self._batch_thread: Optional[threading.Thread] = None

        # Audio health monitoring
        from audio.health_monitor import AudioHealthMonitor
        self._health_monitor = AudioHealthMonitor()

        # Lock to prevent concurrent stop_recording calls (sleep + silence race)
        self._stop_lock = threading.Lock()

        # Silence auto-stop tracking (active during STATE_RECORDING)
        self._recording_silence_start: Optional[float] = None
        self._last_silence_check: float = 0.0

        # Decision popup & checkpoint system
        self._checkpoints: list[RecordingCheckpoint] = []
        self._decision_popup_active: bool = False
        self._milestone_recording_secs: float = 0.0
        self._silence_checkpoint_added: bool = False  # prevent re-adding silence checkpoint while popup open

        # Power monitor (set by main.py)
        self._power_monitor = None

        # Multi-segment recording across sleep/wake cycles
        self._recording_segments: list[Path] = []    # completed WAV segment paths
        self._segment_ogg_paths: list[Path] = []     # parallel OGG paths to clean up
        self._sleeping: bool = False                  # True between sleep and wake
        self._accumulated_recording_secs: float = 0.0  # total recording time pre-sleep
        self._segment_start_time: float = 0.0        # monotonic time when segment started
        self._sleep_wall_time: float = 0.0           # wall-clock time when sleep began

        # Append/replace mode for Fibery transcript/summary writes (per-meeting, not persisted)
        self._transcript_mode: str = "append"  # "append" or "replace"

        # Lock refresh tracking (refresh every 10 min during long recordings)
        self._last_lock_refresh: float = 0.0

        # Session identity token — incremented on reset so background threads
        # can detect that their session is stale and stop firing UI callbacks.
        self._session_token: int = 0

    @property
    def needs_close_confirmation(self) -> bool:
        """True when closing would lose work (recording, unsent transcript, or unsent summary)."""
        if self.state in (self.STATE_RECORDING, self.STATE_PROCESSING):
            return True
        if self.state == self.STATE_COMPLETED and self._session:
            results = self._session.results
            if not self._session.context.entity:
                # Local-only: warn if transcript exists and user hasn't copied it
                return results.get_batch_result() is not None and not results.get_user_has_copied()
            else:
                # Entity linked: warn if transcript or summary not sent yet
                if not results.get_transcript_sent():
                    return True
                # Also warn if a summary was generated but not sent to Fibery
                if results.get_generated_summary() and not results.get_summary_sent():
                    return True
                return False
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
        Increments _session_token so background threads detect staleness.
        """
        self._session_token += 1
        self.release_recording_lock()
        self._validated_entity = None
        self._entity_context = None
        self._session = None
        self._recording_segments = []
        self._segment_ogg_paths = []
        self._sleeping = False
        self._accumulated_recording_secs = 0.0
        self._checkpoints = []
        self._decision_popup_active = False
        self._milestone_recording_secs = 0.0
        self._silence_checkpoint_added = False
        self._transcript_mode = "append"
        self.state = self.STATE_IDLE
        logger.info("Session reset (token=%d)", self._session_token)

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
        if self.state == self.STATE_PROCESSING:
            logger.info("Deselect blocked — processing is active")
            return
        if self._validated_entity:
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

    _RECORDING_SILENCE_DURATION = 60.0    # seconds of silence before decision popup
    _MIN_SLEEP_FOR_CHECKPOINT = 60        # minimum sleep seconds to create checkpoint/popup

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
        if self.state != self.STATE_IDLE:
            raise RuntimeError("Cannot start recording from state: " + self.state)

        # Pause background scanning during recording
        self.stop_background_scanning()

        # Stop monitoring if active
        if self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()

        self._recording_silence_start = None
        self._checkpoints = []
        self._decision_popup_active = False
        self._milestone_recording_secs = 0.0
        self._silence_checkpoint_added = False

        # Reset multi-segment tracking for a fresh recording
        self._recording_segments = []
        self._segment_ogg_paths = []
        self._sleeping = False
        self._accumulated_recording_secs = 0.0
        self._segment_start_time = time.monotonic()

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

        self._health_monitor.reset()
        self._last_lock_refresh = time.monotonic()
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
        # Clear decision popup state if active
        self._decision_popup_active = False
        self._checkpoints = []

        # Release recording lock
        self.release_recording_lock()

        if self._sleeping:
            # User stopped while asleep (via tray/UI) — no active capture to stop
            self._sleeping = False
        else:
            # Stop audio capture
            self.audio_capture.stop_capture()

            # Flush mixer
            if self._mixer:
                self._mixer.flush()
                self._mixer = None

            # Stop WAV recorder and collect this segment
            if self._recorder:
                seg_path = self._recorder.stop()
                if seg_path:
                    self._recording_segments.append(seg_path)
                ogg = self._recorder.compressed_path
                if ogg:
                    self._segment_ogg_paths.append(ogg)
                self._recorder = None

        # Merge segments if we have prior sleep-saved segments
        wav_path, compressed_path = self._finalize_segments()

        # Bake the file paths into the frozen session context.
        # Use current validated entity (user may have switched meetings during recording).
        if self._session and wav_path:
            self._session = RecordingSession(SessionContext(
                entity=self._validated_entity or self._session.context.entity,
                fibery_client=self._fibery_client or self._session.context.fibery_client,
                entity_context=self._entity_context or self._session.context.entity_context,
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

    def _finalize_segments(self) -> tuple:
        """Merge recorded segments and clean up. Returns (wav_path, compressed_path)."""
        segments = self._recording_segments
        ogg_paths = self._segment_ogg_paths
        self._recording_segments = []
        self._segment_ogg_paths = []

        if not segments:
            return None, None

        if len(segments) == 1:
            # Single segment — use directly, keep its OGG if available
            wav_path = segments[0]
            compressed_path = str(ogg_paths[0]) if ogg_paths else None
            return str(wav_path), compressed_path

        # Multiple segments — merge with silence gaps
        from audio.wav_merge import merge_wav_files
        merged_path = merge_wav_files(segments)

        # Clean up individual segment WAV files
        for seg in segments:
            try:
                seg.unlink()
                logger.debug("Cleaned up segment: %s", seg.name)
            except OSError as e:
                logger.warning("Could not delete segment %s: %s", seg.name, e)

        # Clean up segment OGG files (we'll re-compress the merged WAV)
        for ogg in ogg_paths:
            try:
                ogg.unlink()
                logger.debug("Cleaned up segment OGG: %s", ogg.name)
            except OSError as e:
                logger.warning("Could not delete segment OGG %s: %s", ogg.name, e)

        # Return merged path with no compressed_path (force re-compression)
        return str(merged_path), None

    def _finalize_and_process(self) -> None:
        """Merge segments and trigger batch processing.

        Called by wake handler on timeout or device failure.
        """
        try:
            wav_path, compressed_path = self._finalize_segments()
        except Exception as e:
            logger.error("Failed to merge segments: %s", e)
            wav_path, compressed_path = None, None

        if not wav_path:
            self.state = self.STATE_COMPLETED
            self._notify_js("window.onProcessingComplete()")
            return

        # Update session with merged file
        if self._session:
            self._session = RecordingSession(SessionContext(
                entity=self._validated_entity or self._session.context.entity,
                fibery_client=self._fibery_client or self._session.context.fibery_client,
                entity_context=self._entity_context or self._session.context.entity_context,
                wav_path=wav_path,
                compressed_path=compressed_path or "",
                is_uploaded_file=False,
            ))

        self.state = self.STATE_PROCESSING
        logger.info("Finalized segments, starting batch processing")
        self._notify_js("window.onRecordingEndedForProcessing()")

        session = self._session
        if session:
            self._batch_thread = threading.Thread(
                target=self._run_batch_processing,
                args=(session,),
                daemon=True,
            )
            self._batch_thread.start()
        else:
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

        # Throttle JS pushes to ~5/sec to avoid flooding WebView2 with evaluate_js calls.
        # Unthrottled, this fires ~20/sec (both audio sources × 10 callbacks/sec each).
        now = time.monotonic()
        if now - self._last_level_push >= self._LEVEL_PUSH_INTERVAL:
            self._last_level_push = now
            self._notify_js(
                f"window.updateAudioLevels({self._last_mic_level:.4f}, {self._last_sys_level:.4f})"
            )

        # Audio health monitoring during recording
        if self.state == self.STATE_RECORDING:
            health = self._health_monitor.update(mic_level, sys_level)
            if health:
                self._notify_js(
                    f"window.updateAudioHealth && window.updateAudioHealth({json.dumps(health.to_dict())})"
                )
                for warning in self._health_monitor.check_warnings(health):
                    if warning.startswith("BOTH_DEAD:"):
                        # Both channels dead → auto-stop recording
                        msg = warning[len("BOTH_DEAD:"):]
                        self._notify_js(f"window.onHealthWarning && window.onHealthWarning({json.dumps(msg)})")
                        logger.warning("Both audio channels dead, triggering auto-stop")
                        self.stop_recording()
                        self._notify_js("window.onAutoStopComplete()")
                        return
                    self._notify_js(f"window.onHealthWarning && window.onHealthWarning({json.dumps(warning)})")

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

        # Log memory usage every 60 seconds during recording for diagnostics
        if int(now - self._segment_start_time) % 60 < 1:
            try:
                import os
                if hasattr(os, "getpid"):
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    # PROCESS_MEMORY_COUNTERS via GetProcessMemoryInfo
                    class PMC(ctypes.Structure):
                        _fields_ = [("cb", ctypes.c_ulong),
                                    ("PageFaultCount", ctypes.c_ulong),
                                    ("PeakWorkingSetSize", ctypes.c_size_t),
                                    ("WorkingSetSize", ctypes.c_size_t),
                                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                                    ("PagefileUsage", ctypes.c_size_t),
                                    ("PeakPagefileUsage", ctypes.c_size_t)]
                    pmc = PMC()
                    pmc.cb = ctypes.sizeof(PMC)
                    handle = kernel32.GetCurrentProcess()
                    if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                        logger.info(
                            "Memory: RSS=%.1f MB, Peak=%.1f MB (recording for %.0f s)",
                            pmc.WorkingSetSize / 1e6, pmc.PeakWorkingSetSize / 1e6,
                            self._accumulated_recording_secs + (now - self._segment_start_time),
                        )
            except Exception:
                pass  # non-Windows or API unavailable

        # Refresh lock every 10 minutes using the frozen session context.
        # Runs on a separate thread to avoid blocking the audio callback.
        if self._session and self._session.context.entity and self._session.context.fibery_client:
            if now - self._last_lock_refresh > 600:  # 10 minutes
                self._last_lock_refresh = now
                session = self._session  # capture for thread
                def _refresh_lock():
                    try:
                        session.context.fibery_client.set_recording_lock(
                            session.context.entity, self._build_lock_value()
                        )
                        logger.debug("Recording lock refreshed")
                    except Exception:
                        logger.debug("Lock refresh failed", exc_info=True)
                threading.Thread(target=_refresh_lock, daemon=True).start()

        both_silent = (self._last_mic_level < self._SILENCE_THRESHOLD
                       and self._last_sys_level < self._SILENCE_THRESHOLD)

        if both_silent:
            if self._recording_silence_start is None:
                self._recording_silence_start = now
            elapsed = now - self._recording_silence_start

            if elapsed >= self._RECORDING_SILENCE_DURATION:
                if self._decision_popup_active:
                    # Popup already showing — add a silence checkpoint if not already done
                    if not self._silence_checkpoint_added:
                        self._silence_checkpoint_added = True
                        logger.info("Silence detected while decision popup open, adding checkpoint")
                        threading.Thread(
                            target=self._save_milestone_segment, args=("silence",), daemon=True
                        ).start()
                else:
                    # Show decision popup directly (no countdown)
                    self._decision_popup_active = True
                    self._silence_checkpoint_added = True
                    logger.info("Silence detected for %.0fs, showing decision popup", elapsed)
                    threading.Thread(
                        target=self._save_milestone_segment,
                        args=("silence", True), daemon=True
                    ).start()
        else:
            # Audio resumed — reset silence tracking
            self._recording_silence_start = None
            self._silence_checkpoint_added = False

    def _save_milestone_segment(self, checkpoint_type: str, show_popup: bool = False) -> None:
        """Save current segment as a milestone and start a new one.

        Runs on a background thread. Creates a RecordingCheckpoint.
        If show_popup is True, shows the decision popup after creating the checkpoint.
        """
        with self._stop_lock:
            if self.state != self.STATE_RECORDING or not self._recorder:
                return

            # Calculate milestone time (meeting duration excluding silence/sleep gaps)
            segment_elapsed = time.monotonic() - self._segment_start_time
            silence_elapsed = (
                (time.monotonic() - self._recording_silence_start)
                if self._recording_silence_start else 0.0
            )
            useful_secs = max(0, segment_elapsed - silence_elapsed)
            self._milestone_recording_secs = self._accumulated_recording_secs + useful_secs

            # Stop current recorder and save segment
            seg_path = self._recorder.stop()
            if seg_path:
                self._recording_segments.append(seg_path)
            ogg = self._recorder.compressed_path
            if ogg:
                self._segment_ogg_paths.append(ogg)

            # Accumulate full segment time (including silence, for timer accuracy)
            self._accumulated_recording_secs += segment_elapsed

            # Create checkpoint
            checkpoint = RecordingCheckpoint(
                type=checkpoint_type,
                meeting_secs=self._milestone_recording_secs,
                segment_index=len(self._recording_segments),
            )
            self._checkpoints.append(checkpoint)
            logger.info(
                "Checkpoint created: type=%s, meeting_secs=%.1f, segment_index=%d",
                checkpoint_type, self._milestone_recording_secs, checkpoint.segment_index,
            )

            # Start new recorder for background segment
            recordings_dir = (
                Path(self.settings.recordings_dir)
                if self.settings.recordings_dir
                else self.data_dir / "recordings"
            )
            self._recorder = WavRecorder(recordings_dir)
            self._recorder.start()
            self._segment_start_time = time.monotonic()

            # Show or update the decision popup
            if show_popup:
                self._notify_js(
                    f"window.onShowDecisionPopup({json.dumps(self._checkpoints_for_js())})"
                )
            elif self._decision_popup_active:
                self._notify_js(
                    f"window.onDecisionPopupUpdate({json.dumps(self._checkpoints_for_js())})"
                )

    def _checkpoints_for_js(self) -> dict:
        """Build data dict for the decision popup JS."""
        current_secs = self._accumulated_recording_secs + (
            time.monotonic() - self._segment_start_time
        )
        return {
            "checkpoints": [
                {"type": cp.type, "meetingSecs": cp.meeting_secs, "index": i}
                for i, cp in enumerate(self._checkpoints)
            ],
            "currentRecordingSecs": current_secs,
        }

    # --- Decision Popup Actions ---

    def decision_continue_recording(self) -> None:
        """User chose 'Continue Recording' from the decision popup.

        Clears all checkpoints (confirmed as part of the meeting) and resumes.
        """
        self._decision_popup_active = False
        self._checkpoints = []
        self._recording_silence_start = None
        self._silence_checkpoint_added = False

        # Calculate actual accumulated time for timer resume
        current_secs = self._accumulated_recording_secs + (
            time.monotonic() - self._segment_start_time
        )
        logger.info("User chose to continue recording, checkpoints cleared (%.1f s total)", current_secs)
        self._notify_js(f"window.onDecisionTimerResume({current_secs})")

    def decision_end_now(self) -> None:
        """User chose 'End Meeting Now' / 'Process Until Now' from the decision popup.

        Stops recording, merges all segments, and processes.
        """
        self._decision_popup_active = False
        self._checkpoints = []
        logger.info("User chose to end meeting now, processing all segments")
        self.stop_recording()
        self._notify_js("window.onAutoStopComplete()")

    def decision_end_at_checkpoint(self, checkpoint_index: int) -> None:
        """User chose to process up to a specific checkpoint.

        Discards all segments after the checkpoint and processes the rest.
        """
        if checkpoint_index < 0 or checkpoint_index >= len(self._checkpoints):
            logger.error("Invalid checkpoint index: %d", checkpoint_index)
            return

        checkpoint = self._checkpoints[checkpoint_index]
        self._decision_popup_active = False
        self._checkpoints = []
        logger.info(
            "User chose to end at checkpoint %d (%.1f s, segment_index=%d)",
            checkpoint_index, checkpoint.meeting_secs, checkpoint.segment_index,
        )

        with self._stop_lock:
            if self.state != self.STATE_RECORDING:
                return

            # Release recording lock
            self.release_recording_lock()

            # Stop current recording infrastructure
            if not self._sleeping:
                try:
                    self.audio_capture.stop_capture()
                except Exception as e:
                    logger.warning("Error stopping capture: %s", e)
                if self._mixer:
                    self._mixer.flush()
                    self._mixer = None
                # Stop current recorder (discard — it's post-checkpoint audio)
                if self._recorder:
                    discard_path = self._recorder.stop()
                    if discard_path:
                        try:
                            discard_path.unlink()
                        except OSError:
                            pass
                    discard_ogg = self._recorder.compressed_path
                    if discard_ogg:
                        try:
                            discard_ogg.unlink()
                        except OSError:
                            pass
                    self._recorder = None
            else:
                self._sleeping = False

            # Discard segments after the checkpoint
            self._discard_segments_after(checkpoint.segment_index)

        # Process remaining segments
        self._finalize_and_process()
        self._notify_js("window.onAutoStopComplete()")

    def _discard_segments_after(self, keep_count: int) -> None:
        """Delete segment WAV/OGG files after index keep_count and trim lists."""
        discard_segments = self._recording_segments[keep_count:]
        discard_oggs = self._segment_ogg_paths[keep_count:]

        for seg in discard_segments:
            try:
                seg.unlink()
                logger.debug("Discarded post-checkpoint segment: %s", seg.name)
            except OSError as e:
                logger.warning("Could not delete segment %s: %s", seg.name, e)

        for ogg in discard_oggs:
            try:
                ogg.unlink()
                logger.debug("Discarded post-checkpoint OGG: %s", ogg.name)
            except OSError as e:
                logger.warning("Could not delete OGG %s: %s", ogg.name, e)

        self._recording_segments = self._recording_segments[:keep_count]
        self._segment_ogg_paths = self._segment_ogg_paths[:keep_count]

    # --- System Sleep / Wake ---

    def on_system_sleep(self) -> None:
        """Called by power monitor when the system is going to sleep.

        Saves the current segment and pauses. On wake, we auto-resume into a
        new segment. All segments are merged when the user finally stops.
        """
        logger.info("System sleep detected, state=%s", self.state)
        if self.state != self.STATE_RECORDING or self._sleeping:
            return

        # Stop audio capture (devices will be invalid after sleep)
        try:
            self.audio_capture.stop_capture()
        except Exception as e:
            logger.warning("Error stopping capture on sleep: %s", e)

        # Flush mixer and tear down
        if self._mixer:
            self._mixer.flush()
            self._mixer = None

        # Stop recorder and save segment
        if self._recorder:
            seg_path = self._recorder.stop()
            if seg_path:
                self._recording_segments.append(seg_path)
            ogg = self._recorder.compressed_path
            if ogg:
                self._segment_ogg_paths.append(ogg)
            self._recorder = None

        # Accumulate recording time for this segment
        self._accumulated_recording_secs += time.monotonic() - self._segment_start_time

        self._sleeping = True
        self._sleep_wall_time = time.time()

        # Tell JS to freeze the timer
        self._notify_js(f"window.onSleepPauseTimer({self._accumulated_recording_secs})")

    def on_system_wake(self) -> None:
        """Called by power monitor when the system wakes from sleep.

        Always auto-resumes recording. For sleeps ≥ 1 minute, creates a
        checkpoint and shows/updates the decision popup so the user can
        choose to continue, process everything, or process up to a checkpoint.
        """
        logger.info("System wake detected, sleeping=%s, state=%s", self._sleeping, self.state)

        # Re-apply window icon (Win32 icon handles get invalidated after sleep)
        from ui.window import reapply_win32_icon
        threading.Thread(target=reapply_win32_icon, daemon=True).start()

        if not self._sleeping:
            # Warn if processing was likely interrupted by sleep
            if self.state == self.STATE_PROCESSING:
                self._notify_js("window.onSleepDuringProcessing()")
            return

        sleep_duration = time.time() - self._sleep_wall_time
        self._sleeping = False

        # Reset silence tracking BEFORE resume to prevent stale _recording_silence_start
        # from triggering a false silence detection when audio callbacks start firing
        self._recording_silence_start = None
        self._silence_checkpoint_added = False

        # Always try to auto-resume recording
        try:
            self._resume_recording()
        except Exception as e:
            logger.error("Failed to resume recording after wake: %s", e)
            try:
                self._finalize_and_process()
            except Exception as e2:
                logger.error("Finalize also failed: %s", e2)
                self.state = self.STATE_COMPLETED
            self._notify_js(f"window.onWakeResumeFailed({json.dumps(str(e))})")
            return

        # Always resume timer immediately on wake (even for long sleeps).
        # If we show a popup later, the popup JS will freeze the timer at milestone time.
        # This ensures the timer isn't stuck if the popup _notify_js fails.
        self._notify_js(f"window.onWakeResumeTimer({self._accumulated_recording_secs})")

        if sleep_duration >= self._MIN_SLEEP_FOR_CHECKPOINT:
            # Create a sleep checkpoint
            checkpoint = RecordingCheckpoint(
                type="sleep",
                meeting_secs=self._accumulated_recording_secs,
                segment_index=len(self._recording_segments),
            )
            self._checkpoints.append(checkpoint)
            logger.info(
                "Sleep checkpoint: %.0fs asleep, meeting_secs=%.1f, segment_index=%d",
                sleep_duration, checkpoint.meeting_secs, checkpoint.segment_index,
            )

            if self._decision_popup_active:
                # Popup already showing from previous wake — update it immediately
                self._notify_js(
                    f"window.onDecisionPopupUpdate({json.dumps(self._checkpoints_for_js())})"
                )
            else:
                # First popup — delay briefly for display to settle after wake
                self._decision_popup_active = True
                sleep_mins = round(sleep_duration / 60)

                def _show_popup():
                    time.sleep(2)
                    if not self._decision_popup_active:
                        return  # user already dismissed
                    data = self._checkpoints_for_js()
                    data["sleepMinutes"] = sleep_mins
                    self._notify_js(f"window.onShowDecisionPopup({json.dumps(data)})")

                threading.Thread(target=_show_popup, daemon=True).start()
        else:
            # Short sleep — silent resume
            logger.info("Short sleep (%.0fs), auto-resuming silently", sleep_duration)

    def _resume_recording(self) -> None:
        """Reinitialize audio pipeline and start a new recording segment.

        Used after sleep/wake and by decision_continue_recording for sleep popups.
        Raises on failure (caller handles fallback).
        """
        self.audio_capture.reinitialize()

        mic_device = (
            self._find_device(self._selected_mic_index, is_loopback=False)
            if self._selected_mic_index is not None else None
        )
        loopback_device = (
            self._find_device(self._selected_sys_index, is_loopback=True)
            if self._selected_sys_index is not None else None
        )

        if not mic_device and not loopback_device:
            raise RuntimeError("No audio devices found after wake")

        # Start new recorder
        recordings_dir = (
            Path(self.settings.recordings_dir) if self.settings.recordings_dir
            else self.data_dir / "recordings"
        )
        self._recorder = WavRecorder(recordings_dir)
        self._recorder.start()

        # Set up new mixer
        self._mixer = AudioMixer(
            on_mixed_chunk=self._on_mixed_audio,
            has_mic=mic_device is not None,
            has_loopback=loopback_device is not None,
        )

        # Start capture
        self.audio_capture.start_capture(
            mic_device=mic_device,
            loopback_device=loopback_device,
            on_audio_chunk=self._on_audio_chunk,
            on_level_update=self._on_level_update,
        )

        self._segment_start_time = time.monotonic()

        # Reset silence tracking
        self._recording_silence_start = None
        self._silence_checkpoint_added = False

        # Reset health monitor
        self._health_monitor.reset()

        # Refresh Fibery recording lock
        self._last_lock_refresh = time.monotonic()
        if self._session and self._session.context.entity and self._session.context.fibery_client:
            try:
                self._session.context.fibery_client.set_recording_lock(
                    self._session.context.entity, self._build_lock_value()
                )
            except Exception as e:
                logger.warning("Failed to refresh lock on wake: %s", e)

        logger.info("Recording resumed (%.1f s recorded so far)", self._accumulated_recording_secs)

    # --- Batch Processing ---

    def _run_batch_processing(self, session: "RecordingSession") -> None:
        """Run batch transcription with diarization (background thread).

        Receives the session snapshot created at recording stop — immune to
        entity/path changes that happen on the main thread after this is called.
        Uses a captured session_token to detect if reset_session() was called
        while processing, in which case all UI mutations are silently dropped.
        """
        ctx = session.context
        results = session.results
        wav_path = ctx.wav_path
        compressed_path = ctx.compressed_path or None
        token = self._session_token  # snapshot — stale if reset happens

        def _stale():
            return self._session_token != token

        try:
            from transcription.batch import transcribe_with_diarization

            def on_progress(msg):
                logger.info("Batch: %s", msg)
                if not _stale():
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

            results.set_batch_result(result)

            # Session was reset while transcribing — abort all UI work
            if _stale():
                logger.info("Session reset during batch processing, aborting UI updates (token %d→%d)", token, self._session_token)
                return

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

                    # Determine best audio file for multimodal cleanup:
                    # prefer compressed (OGG/FLAC) over WAV for faster upload.
                    cleanup_audio = compressed_path or ""
                    if not cleanup_audio and wav_path:
                        # Check for OGG/FLAC created by _compress_audio during batch
                        for ext in (".ogg", ".flac"):
                            candidate = Path(wav_path).with_suffix(ext)
                            if candidate.exists():
                                cleanup_audio = str(candidate)
                                break
                        else:
                            cleanup_audio = wav_path

                    cleaned = cleanup_transcript(
                        api_key=gemini_key,
                        transcript=raw_text,
                        language=result.get("language", "en"),
                        meeting_context=meeting_context,
                        company_context=self.settings.company_context,
                        model=self.settings.gemini_model_cleanup,
                        audio_path=cleanup_audio,
                    )
                    results.set_cleaned_transcript(cleaned)
                except Exception as e:
                    logger.warning("Transcript cleanup failed, using raw: %s", e)
                    results.set_cleaned_transcript(raw_text)
                    if not _stale():
                        self._notify_js("window.onCleanupFailed()")
            else:
                results.set_cleaned_transcript(raw_text)

            cleaned_transcript = results.get_cleaned_transcript()

            # Final stale check before completing
            if _stale():
                logger.info("Session reset during cleanup, aborting completion (token %d→%d)", token, self._session_token)
                return

            self._notify_js(f"window.setCleanedTranscript({json.dumps(cleaned_transcript)})")

            self.state = self.STATE_COMPLETED
            self._notify_js("window.onProcessingComplete()")

            # Auto-send transcript to Fibery using the frozen entity from session context
            if ctx.entity and ctx.fibery_client:
                threading.Thread(
                    target=self._auto_send_transcript,
                    args=(ctx.entity, ctx.fibery_client, session, token),
                    daemon=True,
                ).start()

            # Upload audio to Fibery if setting is "fibery"
            audio_upload_ok = False
            if self.settings.audio_storage == "fibery":
                audio_upload_ok = self._upload_audio_to_fibery(wav_path, session, token)

            # Post-processing for uploaded (browsed) files:
            # Copy the compressed file to recordings_dir for local backup
            if ctx.is_uploaded_file and self.settings.save_recordings:
                self._copy_compressed_to_recordings(wav_path, compressed_path)

            # Clean up local recordings if user opted out of local storage
            # SAFETY: only delete if no Fibery upload was attempted, or if it succeeded.
            # Otherwise the user would lose their recording with no backup anywhere.
            fibery_upload_attempted = self.settings.audio_storage == "fibery"
            safe_to_delete = not fibery_upload_attempted or audio_upload_ok
            if not ctx.is_uploaded_file and not self.settings.save_recordings and safe_to_delete:
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
            if _stale():
                return
            # Mark as completed even on failure
            self.state = self.STATE_COMPLETED
            self._notify_js(f"window.onBatchFailed({json.dumps({'message': _friendly_error(e), 'wav_path': wav_path or ''})})")
            self._notify_js(f"window.onError({json.dumps(_friendly_error(e))})")
            self.start_background_scanning()

    def _upload_audio_to_fibery(self, wav_path: str, session: "RecordingSession" = None, session_token: int = None) -> bool:
        """Upload the audio recording to the linked Fibery entity's Files field.

        Returns True on success, False on failure or skip.
        If session_token is given, UI callbacks are suppressed when the token
        no longer matches (session was reset).
        """
        # Use session context if available (prevents entity-swap bug)
        entity = session.context.entity if session else self._validated_entity
        client = session.context.fibery_client if session else self._fibery_client
        is_uploaded_file = session.context.is_uploaded_file if session else False

        def _stale():
            return session_token is not None and self._session_token != session_token

        if not entity or not client:
            return False
        if not client.entity_supports_files(entity):
            logger.info(
                "Entity type %s does not support file attachments, skipping",
                entity.database,
            )
            return False
        results = session.results if session else None
        if results and not results.try_start_audio_upload():
            logger.info("Audio upload already in-flight, skipping")
            return False
        try:
            if not _stale():
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
            if not _stale():
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

            return True

        except Exception as e:
            if results:
                results.finish_audio_upload()
            logger.error("Failed to upload audio to Fibery: %s", e)
            if not _stale():
                self._notify_js(
                    f"window.onAudioUploadError({json.dumps(_friendly_error(e))})"
                )
            return False

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

    def retry_send_transcript(self) -> dict:
        """Retry sending transcript to Fibery (called by UI retry button).

        Prefers the currently selected entity over the frozen session entity,
        so the user can re-link a new meeting and retry.
        """
        if not self._session:
            return {"success": False, "error": "No active session"}
        entity = self._validated_entity or self._session.context.entity
        client = self._fibery_client or self._session.context.fibery_client
        if not entity or not client:
            return {"success": False, "error": "No Fibery entity linked"}
        if not self._session.results.try_start_transcript_send():
            return {"success": False, "error": "Send already in progress"}
        try:
            transcript = self._session.results.get_cleaned_transcript()
            if not transcript:
                batch = self._session.results.get_batch_result()
                if batch and batch.get("utterances"):
                    transcript = format_diarized_transcript(batch["utterances"])
            if not transcript:
                self._session.results.finish_transcript_send(success=False)
                return {"success": False, "error": "No transcript available"}
            client.update_transcript_only(entity, transcript, append=(self._transcript_mode == "append"))
            self._session.results.finish_transcript_send(success=True)
            self._notify_js("window.onTranscriptSentToFibery()")
            return {"success": True}
        except Exception as e:
            self._session.results.finish_transcript_send(success=False)
            return {"success": False, "error": _friendly_error(e)}

    def retry_audio_upload(self) -> dict:
        """Retry uploading audio to Fibery (called by UI retry button).

        Always uses the currently validated entity/client so retries route
        consistently even after re-linking a different meeting.
        """
        if not self._session:
            return {"success": False, "error": "No active session"}
        wav_path = self._session.context.wav_path
        if not wav_path:
            return {"success": False, "error": "No recording available"}
        if not Path(wav_path).exists():
            return {"success": False, "error": "Recording file was deleted. Cannot retry upload."}
        entity = self._validated_entity or self._session.context.entity
        client = self._fibery_client or self._session.context.fibery_client
        if not entity or not client:
            return {"success": False, "error": "No Fibery entity linked"}
        # Build a proxy session targeting the current entity
        retry_session = RecordingSession(SessionContext(
            entity=entity,
            fibery_client=client,
            entity_context=self._entity_context,
            wav_path=wav_path,
            compressed_path=self._session.context.compressed_path,
            is_uploaded_file=self._session.context.is_uploaded_file,
        ))
        retry_session.results = self._session.results
        self._upload_audio_to_fibery(wav_path, retry_session)
        return {"success": True}

    def _auto_send_transcript(self, entity, fibery_client, session: "RecordingSession" = None, session_token: int = None) -> None:
        """Send the current transcript to the Fibery Transcript field (background thread).

        Args:
            entity: The FiberyEntity to send to.
            fibery_client: The FiberyClient to use.
            session: If provided, reads transcript from session.results and marks sent.
            session_token: If provided, UI callbacks are suppressed when stale.
        """
        def _stale():
            return session_token is not None and self._session_token != session_token

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
            if self._transcript_mode == "append":
                fibery_client.update_transcript_only(entity, transcript_text, append=True)
            else:
                fibery_client.update_transcript_only(entity, transcript_text)
            if results:
                results.finish_transcript_send(success=True)
            if not _stale():
                self._notify_js("window.onTranscriptSentToFibery()")
            logger.info("Transcript auto-sent to Fibery (mode=%s)", self._transcript_mode)
        except Exception as exc:
            if results:
                results.finish_transcript_send(success=False)
            logger.error("Auto-send transcript to Fibery failed: %s", exc)
            if not _stale():
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

        # Block before creating the entity to avoid orphans in Fibery
        if self.state == self.STATE_PROCESSING:
            return {'success': False, 'error': 'Cannot change meeting while processing is active. Please wait until processing completes.'}

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

            # Release lock on old entity before switching (during recording)
            if self.state == self.STATE_RECORDING and self._validated_entity:
                try:
                    self.release_recording_lock()
                except Exception:
                    logger.debug("Failed to release old lock on meeting switch", exc_info=True)

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

            # Block re-linking during processing — background threads depend on frozen session.
            if self.state == self.STATE_PROCESSING:
                client.close()
                return {"success": False, "error": "Cannot change meeting while processing is active. Please wait until processing completes."}

            # Release lock on old entity before switching (during recording)
            if self.state == self.STATE_RECORDING and self._validated_entity:
                try:
                    self.release_recording_lock()
                except Exception:
                    logger.debug("Failed to release old lock on meeting switch", exc_info=True)

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

            # If audio storage is Fibery and we have a recording, upload it now.
            # Build a proxy session with the NEW entity so audio lands on the
            # same target as the transcript (unified post-hoc routing).
            if self.settings.audio_storage == "fibery" and self._session and self._session.context.wav_path:
                upload_session = RecordingSession(SessionContext(
                    entity=entity,
                    fibery_client=client,
                    entity_context=self._entity_context,
                    wav_path=self._session.context.wav_path,
                    compressed_path=self._session.context.compressed_path,
                    is_uploaded_file=self._session.context.is_uploaded_file,
                ))
                upload_session.results = self._session.results
                threading.Thread(
                    target=self._upload_audio_to_fibery,
                    args=(self._session.context.wav_path, upload_session),
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

        The summary is cached in session.results for later use.
        If a Fibery entity is already validated, also sends the summary there.
        """
        from integrations.gemini_client import summarize_transcript

        # Capture session and entity locally — prevents TOCTOU if reset_session()
        # runs concurrently on another thread.
        session = self._session
        session_results = session.results if session else None
        entity = self._validated_entity
        client = self._fibery_client
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
            if entity and client:
                try:
                    notes = client.get_entity_notes(entity)
                    is_interview = entity.database.lower() == "market interview"
                except Exception:
                    pass
                # Build dynamic meeting context from entity
                entity_ctx = self._fetch_entity_context()
                if entity_ctx:
                    from integrations.context_builder import build_summary_context
                    meeting_context = build_summary_context(entity_ctx)

            logger.info("Generating summary (style=%s, has_entity=%s)", summary_style, bool(entity))

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
            if entity and client:
                if session_results and not session_results.try_start_summary_send():
                    logger.info("Summary send already in-flight, returning generated summary")
                    return {"success": True, "sent_to_fibery": False, "summary": summary}
                try:
                    client.update_summary_only(entity, ai_summary=summary, append=(self._transcript_mode == "append"))
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
        # Capture locally to prevent TOCTOU
        session = self._session
        session_results = session.results if session else None
        entity = self._validated_entity
        client = self._fibery_client
        summary = session_results.get_generated_summary() if session_results else None
        if not summary:
            return {"success": False, "error": "No summary available"}
        if not entity or not client:
            return {"success": False, "error": "No Fibery entity validated"}
        if session_results and not session_results.try_start_summary_send():
            logger.info("Summary send already in-flight, skipping")
            return {"success": False, "error": "Summary send already in progress"}
        try:
            client.update_summary_only(entity, ai_summary=summary)
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

        # Capture locally to prevent TOCTOU
        session = self._session
        session_results = session.results if session else None
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

            if session_results:
                session_results.set_generated_summary(summary)
            client.update_summary_only(entity, ai_summary=summary)
            if session_results:
                session_results.finish_summary_send(success=True)
            logger.info("AI Summary updated in Fibery")
            return {"success": True}

        except Exception as e:
            if session_results:
                session_results.finish_summary_send(success=False)
            logger.error("Fibery summarize workflow failed: %s", e)
            return {"success": False, "error": _friendly_error(e)}

    # --- UI Communication ---

    def _emergency_stop_recording(self) -> None:
        """Stop recording and save files without triggering batch processing.

        Used during shutdown to ensure WAV/OGG files are properly finalized
        (headers written, files closed) even though we won't transcribe.
        Handles active recording, sleeping, and decision popup states.
        """
        with self._stop_lock:
            if self.state != self.STATE_RECORDING:
                return
            logger.info("Emergency stop: saving recording files before shutdown")

            # Clear popup state
            self._decision_popup_active = False
            self._checkpoints = []

            if self._sleeping:
                # Already paused — just merge saved segments
                self._sleeping = False
            else:
                # Active recording — stop capture and save current segment
                try:
                    self.audio_capture.stop_capture()
                except Exception as e:
                    logger.warning("Error stopping capture during emergency: %s", e)
                if self._mixer:
                    self._mixer.flush()
                    self._mixer = None
                if self._recorder:
                    seg_path = self._recorder.stop()
                    if seg_path:
                        self._recording_segments.append(seg_path)
                    self._recorder = None

            # Merge all segments into a single file
            if self._recording_segments:
                try:
                    self._finalize_segments()
                except Exception as e:
                    logger.warning("Failed to merge segments during emergency stop: %s", e)

            self.state = self.STATE_IDLE

    def begin_shutdown(self) -> None:
        """Mark app as shutting down and stop all background activity."""
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        self._emergency_stop_recording()
        self.release_recording_lock()
        # Wait briefly for batch processing to finish (avoid losing transcript)
        if self._batch_thread and self._batch_thread.is_alive():
            logger.info("Waiting for batch processing to complete...")
            self._batch_thread.join(timeout=5.0)
            if self._batch_thread.is_alive():
                logger.warning("Batch processing still running, forcing shutdown")
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
