"""Application orchestrator. Manages state, coordinates audio, transcription, and UI."""

import copy
import getpass
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from audio.capture import AudioCapture, AudioDevice, create_audio_capture
from audio.device_scanner import scan_all_devices
from audio.file_formats import (
    FFMPEG_BACKED_AUDIO_FORMATS,
    SUPPORTED_UPLOADED_AUDIO_EXTENSIONS,
    load_audio_segment,
    missing_ffmpeg_tools,
)
from audio.health_monitor import SPEECH_THRESHOLD
from audio.mixer import AudioMixer
from audio.recorder import WavRecorder
from config.constants import FIBERY_INSTANCE_URL
from config.keystore import get_key, keys_configured
from config.settings import Settings
from config.session import RecordingSession, SessionContext
from transcription.formatter import format_diarized_transcript
from utils.filename_utils import (
    PLACEHOLDER_RECORDING_STEM_RE,
    RECORDING_PREFIX_RE,
    append_counter,
    build_recording_stem,
    sanitize_name,
    truncate_stem_for_directory,
)

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


@dataclass(frozen=True)
class TranscriptionOptions:
    """Per-run transcription controls from the Transcribe section."""

    remove_echo: bool = False
    improve_with_context: bool = True
    transcript_mode: str = "append"
    recording_mode: str = "mic_only"


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
    STATE_PREPARED = "prepared"
    STATE_PROCESSING = "processing"
    STATE_COMPLETED = "completed"

    # Runtime fields that should be reset whenever staged/recording workflow state
    # is discarded or reconstructed from an undo snapshot.
    _WORKFLOW_RUNTIME_DEFAULTS = {
        "_recording_segments": [],
        "_segment_ogg_paths": [],
        "_sleeping": False,
        "_accumulated_recording_secs": 0.0,
        "_recording_channels": None,
        "_checkpoints": [],
        "_decision_popup_active": False,
        "_milestone_recording_secs": 0.0,
        "_silence_checkpoint_added": False,
        "_recording_silence_start": None,
        "_segment_start_time": 0.0,
    }

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

        # Active session for the currently staged or processed audio file.
        self._session: Optional[RecordingSession] = None

        # Cached Fibery validation (UI-level; may change while session is live)
        self._validated_entity = None
        self._fibery_client = None
        # Entity context for AssemblyAI keyterms / summary enrichment
        self._entity_context = None
        self._linked_transcript_text: str = ""

        # Level update throttling — limit evaluate_js calls to ~5/sec
        self._last_mic_level: float = 0.0        # noise-suppressed (for speech detection)
        self._last_raw_mic_level: float = 0.0    # raw pre-noise-suppression (for silence/health)
        self._last_sys_level: float = 0.0
        self._last_level_push: float = 0.0
        self._LEVEL_PUSH_INTERVAL: float = 0.2  # seconds between JS level pushes

        # Queue to decouple PortAudio/loopback callbacks from JS dispatch and health processing.
        # Audio callbacks put_nowait() here; _level_dispatch_loop drains on a background thread.
        self._level_queue: queue.Queue = queue.Queue(maxsize=100)
        self._is_shutting_down = False  # must be set before dispatch thread starts

        # Device scanning
        self._scan_thread: Optional[threading.Thread] = None
        self._scan_stop_event = threading.Event()
        self._silence_counter_mic: int = 0   # consecutive silent ticks
        self._silence_counter_sys: int = 0
        self._selected_mic_index: Optional[int] = None
        self._selected_sys_index: Optional[int] = None
        self._monitor_include_loopback: bool = False
        # Periodic idle rescans are disabled in favor of explicit one-shot
        # auto-detect runs from the UI when the Recording tab becomes active
        # or the user manually refreshes devices there.
        self._background_scanning_enabled: bool = False
        self._tray_quit_requested = False
        self._batch_thread: Optional[threading.Thread] = None

        # Audio health monitoring
        from audio.health_monitor import AudioHealthMonitor
        self._health_monitor = AudioHealthMonitor()

        # Lock to serialize monitor/record/upload/transcribe capture transitions.
        self._audio_lifecycle_lock = threading.RLock()

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
        self._recording_channels: Optional[int] = None  # fixed output format for current recording

        # Append/replace mode for Fibery transcript writes (per-meeting, not persisted)
        self._transcript_mode: str = "append"  # "append" or "replace"
        # Recording mode for AssemblyAI routing (per-meeting, not persisted)
        self._recording_mode: str = "mic_only"  # "mic_only" or "mic_and_speakers"
        # Append/replace mode for Fibery summary writes (per-meeting, not persisted)
        self._summary_mode: str = "append"  # "append" or "replace"
        # Summary output language (per-meeting, not persisted)
        self._summary_language: str = "en"  # "en" or "nl"

        # Lock refresh tracking (refresh every 10 min during long recordings)
        self._last_lock_refresh: float = 0.0

        # Session identity token — incremented on reset so background threads
        # can detect that their session is stale and stop firing UI callbacks.
        self._session_token: int = 0
        self._prepared_audio_info: Optional[dict] = None
        self._undo_snapshot: Optional[dict] = None
        self._undo_snapshot_expires_at: float = 0.0

        # Start level dispatch thread last — it reads fields initialized above.
        self._level_dispatch_thread = threading.Thread(
            target=self._level_dispatch_loop, daemon=True, name="level-dispatch"
        )
        self._level_dispatch_thread.start()

    @property
    def needs_close_confirmation(self) -> bool:
        """True when closing would lose work (recording, unsent transcript, or unsent summary)."""
        if self.state in (self.STATE_RECORDING, self.STATE_PREPARED, self.STATE_PROCESSING):
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

    def _get_recordings_dir(self) -> Path:
        """Return the active local recordings directory."""
        if self.settings.recordings_dir:
            return Path(self.settings.recordings_dir).expanduser()
        return self.data_dir / "recordings"

    def _path_is_within(self, path: Path, directory: Path) -> bool:
        """Return True when *path* is inside *directory*."""
        try:
            path.resolve(strict=False).relative_to(directory.resolve(strict=False))
            return True
        except ValueError:
            return False

    def _get_meeting_name(self) -> str:
        """Return sanitized meeting name or 'recording' as fallback."""
        raw = getattr(self._validated_entity, "entity_name", "") or ""
        return sanitize_name(raw) if raw.strip() else "recording"

    def _build_unique_recordings_path(
        self,
        suffix: str,
        recordings_dir: Path,
        meeting_name: Optional[str] = None,
        original_filename: Optional[str] = None,
    ) -> Path:
        """Return a non-conflicting destination path inside recordings_dir.

        Naming convention:
        - Recording:  yyyymmdd_hhmm_[meeting-name]_[#].ext
        - Copy:       yyyymmdd_hhmm_[meeting-name]_[original-name]_[#].ext
        - Copy with existing convention: reuse source stem, iterate #
        """
        name = meeting_name or "recording"

        if original_filename:
            orig_stem = Path(original_filename).stem
            # If source already follows our date convention, reuse its stem as-is
            if RECORDING_PREFIX_RE.match(orig_stem):
                base_stem = orig_stem
            else:
                sanitized_orig = sanitize_name(orig_stem)
                # When no meeting is selected the fallback name is "recording".
                # Skip it from the stem to avoid "recording_recording_..." when
                # the original filename already starts with "recording".
                if name == "recording" and sanitized_orig.startswith("recording"):
                    from datetime import datetime
                    base_stem = f"{datetime.now().strftime('%Y%m%d_%H%M')}_{sanitized_orig}"
                else:
                    base_stem = f"{build_recording_stem(name)}_{sanitized_orig}"
        else:
            base_stem = build_recording_stem(name)

        base_stem = truncate_stem_for_directory(base_stem, recordings_dir, suffix)

        candidate = recordings_dir / f"{base_stem}{suffix}"
        if not candidate.exists():
            return candidate

        counter = 2
        while True:
            candidate = recordings_dir / f"{base_stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _recording_stem_with_counter(base_stem: str, counter: Optional[int]) -> str:
        """Append a numeric suffix when one is required."""
        return append_counter(base_stem, counter)

    def _build_selected_entity_recording_stem(self, source_path: Path, meeting_name: str) -> tuple[str, Optional[int]] | None:
        """Return the renamed base stem and preserved numeric suffix for placeholder recordings."""
        match = PLACEHOLDER_RECORDING_STEM_RE.match(source_path.stem)
        if not match:
            return None

        merged_prefix = match.group("merged") or ""
        timestamp_prefix = match.group("prefix")
        counter_text = match.group("counter")
        preferred_counter = int(counter_text[1:]) if counter_text else None
        base_stem = f"{merged_prefix}{timestamp_prefix}_{meeting_name}"
        base_stem = truncate_stem_for_directory(base_stem, source_path.parent, source_path.suffix)
        return base_stem, preferred_counter

    def _recording_stem_is_available(
        self,
        directory: Path,
        candidate_stem: str,
        wav_source: Path,
        ogg_source: Optional[Path],
    ) -> bool:
        """Return True when a shared WAV/OGG stem is free to use."""
        files_to_check: list[tuple[str, Path]] = [(".wav", wav_source)]
        if ogg_source is not None:
            files_to_check.append((".ogg", ogg_source))

        for suffix, source in files_to_check:
            candidate = directory / f"{candidate_stem}{suffix}"
            if candidate.exists() and candidate != source:
                return False
        return True

    def _choose_selected_entity_recording_stem(
        self,
        wav_source: Path,
        ogg_source: Optional[Path],
        base_stem: str,
        preferred_counter: Optional[int],
    ) -> str:
        """Pick a non-conflicting shared stem for renamed staged recording files."""
        candidate_stem = self._recording_stem_with_counter(base_stem, preferred_counter)
        if self._recording_stem_is_available(wav_source.parent, candidate_stem, wav_source, ogg_source):
            return candidate_stem

        counter = preferred_counter + 1 if preferred_counter is not None else 2
        while True:
            candidate_stem = f"{base_stem}_{counter}"
            if self._recording_stem_is_available(wav_source.parent, candidate_stem, wav_source, ogg_source):
                return candidate_stem
            counter += 1

    @staticmethod
    def _rename_paths_with_rollback(rename_pairs: list[tuple[Path, Path]]) -> None:
        """Rename files and roll back any earlier moves if a later move fails."""
        renamed: list[tuple[Path, Path]] = []
        try:
            for source, target in rename_pairs:
                if source == target:
                    continue
                source.rename(target)
                renamed.append((target, source))
        except OSError:
            for current_path, original_path in reversed(renamed):
                try:
                    current_path.rename(original_path)
                except OSError:
                    logger.warning(
                        "Failed to roll back renamed staged audio file %s -> %s",
                        current_path,
                        original_path,
                    )
            raise

    def _refresh_staged_session_audio_paths(self, wav_path: str, compressed_path: str) -> None:
        """Update the staged session paths while preserving the existing results object."""
        if not self._session:
            return

        current_session = self._session
        ctx = current_session.context
        self._session = RecordingSession(SessionContext(
            entity=ctx.entity,
            fibery_client=ctx.fibery_client,
            entity_context=ctx.entity_context,
            wav_path=wav_path,
            compressed_path=compressed_path,
            is_uploaded_file=ctx.is_uploaded_file,
        ))
        self._session.results = current_session.results
        self._prepared_audio_info = self._build_prepared_audio_info(
            Path(wav_path),
            ctx.is_uploaded_file,
        )

    def _rename_placeholder_recording_for_selected_entity(self) -> None:
        """Rename staged recorded audio from the placeholder stem to the selected entity name."""
        session = self._session
        entity = self._validated_entity
        if not session or session.context.is_uploaded_file or not entity:
            return

        wav_source = Path(session.context.wav_path)
        if not wav_source.exists():
            return

        meeting_name = self._get_meeting_name()
        stem_info = self._build_selected_entity_recording_stem(wav_source, meeting_name)
        if not stem_info:
            return

        compressed_text = session.context.compressed_path or ""
        compressed_source = Path(compressed_text) if compressed_text else None
        raw_ogg_source = None
        if (
            compressed_source
            and compressed_source.suffix.lower() == ".ogg"
            and compressed_source.parent == wav_source.parent
            and compressed_source.stem == wav_source.stem
        ):
            raw_ogg_source = compressed_source
        else:
            candidate_ogg = wav_source.with_suffix(".ogg")
            if candidate_ogg.exists():
                raw_ogg_source = candidate_ogg

        base_stem, preferred_counter = stem_info
        target_stem = self._choose_selected_entity_recording_stem(
            wav_source,
            raw_ogg_source,
            base_stem,
            preferred_counter,
        )
        target_wav = wav_source.parent / f"{target_stem}{wav_source.suffix}"
        target_ogg = target_wav.with_suffix(".ogg") if (compressed_text or raw_ogg_source) else None
        if target_wav == wav_source and (not raw_ogg_source or target_ogg == raw_ogg_source):
            return

        rename_pairs: list[tuple[Path, Path]] = []
        if raw_ogg_source and target_ogg and raw_ogg_source.exists() and raw_ogg_source != target_ogg:
            rename_pairs.append((raw_ogg_source, target_ogg))
        if wav_source != target_wav:
            rename_pairs.append((wav_source, target_wav))
        if not rename_pairs:
            return

        try:
            self._rename_paths_with_rollback(rename_pairs)
        except OSError as e:
            logger.warning("Could not rename staged recording for selected meeting: %s", e)
            return

        updated_compressed_path = compressed_text
        if target_ogg and (compressed_text or raw_ogg_source):
            updated_compressed_path = str(target_ogg)
        self._refresh_staged_session_audio_paths(str(target_wav), updated_compressed_path)
        logger.info("Renamed staged recording for selected meeting: %s -> %s", wav_source.name, target_wav.name)

    def _copy_uploaded_file_to_recordings(self, file_path: Path) -> Path:
        """Copy an imported file into the recordings folder when it lives elsewhere."""
        import shutil

        if not self.settings.save_recordings:
            return file_path

        recordings_dir = self._get_recordings_dir()
        if self._path_is_within(file_path, recordings_dir):
            return file_path

        try:
            recordings_dir.mkdir(parents=True, exist_ok=True)
            meeting_name = self._get_meeting_name()
            destination = self._build_unique_recordings_path(
                suffix=file_path.suffix,
                recordings_dir=recordings_dir,
                meeting_name=meeting_name,
                original_filename=file_path.name,
            )
            shutil.copy2(str(file_path), str(destination))
            logger.info("Copied uploaded audio to recordings folder: %s", destination.name)
            return destination
        except OSError as e:
            logger.warning("Could not copy uploaded audio to recordings dir: %s", e)
            return file_path

    def _build_prepared_audio_info(self, file_path: Path, is_uploaded_file: bool) -> dict:
        """Return file-card metadata for a prepared recording or uploaded file."""
        info = self._validate_audio_file(file_path) or {}
        if not isinstance(info, dict):
            info = {}
        recording_mode_meta = self._recommend_recording_mode(
            file_path,
            int(info.get("channels", 1) or 1),
            is_uploaded_file=is_uploaded_file,
        )
        info.update({
            "file_path": str(file_path),
            "file_name": file_path.name,
            "is_uploaded_file": is_uploaded_file,
            "can_remove_echo": info.get("channels", 1) >= 2,
            **recording_mode_meta,
        })
        return info

    def _classify_stereo_layout_from_samples(self, left_channel, right_channel) -> str:
        """Classify whether stereo channels are duplicated, silent, or distinct."""
        import numpy as np

        left = np.asarray(left_channel, dtype=np.float32).reshape(-1)
        right = np.asarray(right_channel, dtype=np.float32).reshape(-1)
        sample_count = min(left.size, right.size)
        if sample_count == 0:
            return "split_stereo"

        left = left[:sample_count]
        right = right[:sample_count]

        left_rms = float(np.sqrt(np.mean(np.square(left))))
        right_rms = float(np.sqrt(np.mean(np.square(right))))
        louder = max(left_rms, right_rms, 1e-9)
        quieter_ratio = min(left_rms, right_rms) / louder
        if quieter_ratio <= 0.15:
            return "single_sided_stereo"

        left_centered = left - float(np.mean(left))
        right_centered = right - float(np.mean(right))
        left_std = float(np.std(left_centered))
        right_std = float(np.std(right_centered))
        if left_std < 1e-6 and right_std < 1e-6:
            correlation = 1.0 if np.allclose(left, right) else 0.0
        elif left_std < 1e-6 or right_std < 1e-6:
            correlation = 0.0
        else:
            correlation = float(np.corrcoef(left_centered, right_centered)[0, 1])

        if correlation >= 0.98 and quieter_ratio >= 0.85:
            return "dual_mono"
        return "split_stereo"

    def _analyze_uploaded_stereo_layout(self, file_path: Path) -> str:
        """Inspect an uploaded stereo file to see if channels are actually distinct."""
        import numpy as np

        max_frames = 16000 * 90
        try:
            import soundfile as sf

            with sf.SoundFile(str(file_path), "r") as src:
                if int(src.channels) < 2:
                    return "mono"
                frames_to_read = min(len(src), max_frames)
                sample_data = src.read(frames_to_read, dtype="float32", always_2d=True)
                if sample_data.shape[1] < 2:
                    return "mono"
                return self._classify_stereo_layout_from_samples(sample_data[:, 0], sample_data[:, 1])
        except Exception:
            logger.debug("soundfile stereo layout analysis failed for %s", file_path, exc_info=True)

        try:
            audio = load_audio_segment(file_path)
            if audio.channels < 2:
                return "mono"
            audio = audio[:90000]
            mono_tracks = audio.split_to_mono()
            if len(mono_tracks) < 2:
                return "mono"
            scale = float(1 << (8 * mono_tracks[0].sample_width - 1))
            left = np.array(mono_tracks[0].get_array_of_samples(), dtype=np.float32) / scale
            right = np.array(mono_tracks[1].get_array_of_samples(), dtype=np.float32) / scale
            return self._classify_stereo_layout_from_samples(left, right)
        except Exception:
            logger.debug("pydub stereo layout analysis failed for %s", file_path, exc_info=True)

        return "split_stereo"

    def _recommend_recording_mode(self, file_path: Path, channels: int, *, is_uploaded_file: bool) -> dict:
        """Recommend the best AssemblyAI routing mode for the staged audio."""
        if channels <= 1:
            return {
                "stereo_layout": "mono",
                "recording_mode_recommendation": "mic_only",
                "recording_mode_reason": "Mono audio cannot be split into separate mic and speaker channels.",
            }

        if not is_uploaded_file:
            if self._recording_channels == 2:
                return {
                    "stereo_layout": "split_stereo",
                    "recording_mode_recommendation": "mic_and_speakers",
                    "recording_mode_reason": "This recording captured mic and speakers as separate channels.",
                }
            return {
                "stereo_layout": "mono",
                "recording_mode_recommendation": "mic_only",
                "recording_mode_reason": "This recording did not capture a loopback/speaker channel.",
            }

        stereo_layout = self._analyze_uploaded_stereo_layout(file_path)
        if stereo_layout == "dual_mono":
            return {
                "stereo_layout": stereo_layout,
                "recording_mode_recommendation": "mic_only",
                "recording_mode_reason": "The stereo channels look nearly identical, so speakers mode would duplicate the transcript.",
            }
        if stereo_layout == "single_sided_stereo":
            return {
                "stereo_layout": stereo_layout,
                "recording_mode_recommendation": "mic_only",
                "recording_mode_reason": "One stereo channel is mostly silent, so speakers mode would not add useful transcript data.",
            }
        return {
            "stereo_layout": stereo_layout,
            "recording_mode_recommendation": "mic_and_speakers",
            "recording_mode_reason": "The stereo channels appear distinct enough for separate mic and speaker transcription.",
        }

    def _normalize_recording_mode(self, requested_mode: str) -> tuple[str, bool, str]:
        """Auto-correct obvious invalid recording-mode selections."""
        normalized = requested_mode if requested_mode in ("mic_only", "mic_and_speakers") else self._recording_mode
        prepared = self._prepared_audio_info or {}
        recommendation = prepared.get("recording_mode_recommendation")
        reason = prepared.get("recording_mode_reason", "")
        if normalized == "mic_and_speakers" and recommendation == "mic_only":
            logger.info("Auto-correcting recording mode to mic_only: %s", reason or "obvious mismatch")
            return "mic_only", True, reason
        return normalized, False, ""

    def _normalize_summary_language(self, summary_language: str) -> str:
        """Normalize summary output language to a supported code."""
        return "nl" if (summary_language or "").strip().lower() == "nl" else "en"

    def _set_prepared_session(
        self,
        *,
        wav_path: str,
        compressed_path: str = "",
        is_uploaded_file: bool,
        entity=None,
        fibery_client=None,
        entity_context=None,
    ) -> dict:
        """Stage an audio file for later transcription."""
        file_path = Path(wav_path)
        self._session = RecordingSession(SessionContext(
            entity=entity,
            fibery_client=fibery_client,
            entity_context=entity_context,
            wav_path=wav_path,
            compressed_path=compressed_path,
            is_uploaded_file=is_uploaded_file,
        ))
        self._prepared_audio_info = self._build_prepared_audio_info(file_path, is_uploaded_file)
        self._recording_mode = self._prepared_audio_info.get("recording_mode_recommendation", "mic_only")
        self.state = self.STATE_PREPARED
        self._resume_background_scanning()
        return self._prepared_audio_info

    def clear_prepared_audio(self) -> None:
        """Discard staged audio while keeping the linked meeting intact."""
        if self.state != self.STATE_PREPARED or not self._session:
            return
        self._session = None
        self._prepared_audio_info = None
        self.state = self.STATE_IDLE
        self._resume_background_scanning()

    def _snapshot_session_for_transcription(self, session: RecordingSession) -> RecordingSession:
        """Freeze the currently selected meeting into the staged audio session."""
        ctx = session.context
        snapshot = RecordingSession(SessionContext(
            entity=self._validated_entity or ctx.entity,
            fibery_client=self._fibery_client or ctx.fibery_client,
            entity_context=self._entity_context or ctx.entity_context,
            wav_path=ctx.wav_path,
            compressed_path=ctx.compressed_path,
            is_uploaded_file=ctx.is_uploaded_file,
        ))
        snapshot.results = session.results
        return snapshot

    def _notify_audio_prepared(self, info: Optional[dict]) -> None:
        """Notify the UI that an audio file is staged and ready to transcribe."""
        payload = info or {}
        self._notify_js(f"window.onAudioPrepared({json.dumps(payload)})")

    def _build_post_process_settings(self) -> Optional[dict]:
        """Return post-processing stage toggles, or None when disabled."""
        if not self.settings.post_processing:
            return None

        return {
            "echo_cancel": self.settings.echo_cancellation,
            "noise_suppress": self.settings.post_noise_suppression,
            "agc": self.settings.post_agc,
            "normalize": self.settings.post_normalize,
        }

    def _set_power_state(self, prevent_sleep: bool) -> None:
        """Windows-only: prevent or allow system sleep via SetThreadExecutionState.

        Thread-lifetime caveat: Windows clears ES_CONTINUOUS when the calling
        thread exits. All call sites below run on the long-lived app / JS bridge
        thread — do NOT move this call onto a short-lived worker thread or the
        assertion evaporates when that thread returns.
        """
        if sys.platform != "win32":
            return
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        try:
            if prevent_sleep:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                )
                logger.debug("Sleep prevention enabled")
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                logger.debug("Sleep prevention cleared")
        except Exception as e:
            logger.debug("SetThreadExecutionState failed: %s", e)

    def _build_level_monitor_noise_suppressor(self):
        """Create the optional mic suppressor used for speech detection."""
        if not self.settings.noise_suppression:
            return None

        from audio.noise_suppressor import NoiseSuppressor

        noise_suppressor = NoiseSuppressor(enabled=True)
        if not noise_suppressor.available:
            return None
        return noise_suppressor

    def _build_ogg_processors(self) -> tuple[object | None, object | None]:
        """Create fresh processors for the parallel OGG writer."""
        ogg_noise_suppressor = None
        ogg_agc = None

        if self.settings.noise_suppression:
            from audio.noise_suppressor import NoiseSuppressor

            ogg_noise_suppressor = NoiseSuppressor(enabled=True)
            if not ogg_noise_suppressor.available:
                ogg_noise_suppressor = None

        if self.settings.agc:
            from audio.agc import AutomaticGainControl

            ogg_agc = AutomaticGainControl(enabled=True)

        return ogg_noise_suppressor, ogg_agc

    def _start_recorder(self, channels: Optional[int] = None) -> Path:
        """Start a recorder using the session's fixed channel layout."""
        resolved_channels = channels
        if resolved_channels is None:
            resolved_channels = self._recording_channels
        if resolved_channels is None:
            resolved_channels = self._mixer.channels if self._mixer else 1

        ogg_noise_suppressor, ogg_agc = self._build_ogg_processors()
        self._recorder = WavRecorder(
            self._get_recordings_dir(),
            noise_suppressor=ogg_noise_suppressor,
            agc=ogg_agc,
            channels=resolved_channels,
            meeting_name=self._get_meeting_name(),
        )
        return self._recorder.start()

    @staticmethod
    def _clone_optional_data(value):
        """Best-effort clone for snapshot data."""
        if value is None:
            return None
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    def _clear_undo_snapshot(self) -> None:
        self._undo_snapshot = None
        self._undo_snapshot_expires_at = 0.0

    def _reset_workflow_runtime_state(self) -> None:
        """Reset transient recording/session runtime fields to safe defaults."""
        for field_name, default_value in self._WORKFLOW_RUNTIME_DEFAULTS.items():
            setattr(self, field_name, copy.deepcopy(default_value))

    def _prune_expired_undo_snapshot(self) -> None:
        if (
            self._undo_snapshot
            and self._undo_snapshot_expires_at
            and time.monotonic() >= self._undo_snapshot_expires_at
        ):
            self._clear_undo_snapshot()

    def _discard_paths(self, paths) -> None:
        for raw_path in paths:
            if not raw_path:
                continue
            try:
                Path(raw_path).unlink()
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.debug("Could not delete temporary audio file %s: %s", raw_path, e)

    def _cleanup_session_audio_files(self, session: Optional[RecordingSession]) -> None:
        if not session or session.context.is_uploaded_file:
            return
        self._discard_paths([session.context.compressed_path, session.context.wav_path])

    def _build_undo_snapshot(self) -> dict:
        return {
            "state": self.state,
            "session": self._session.clone() if self._session else None,
            "prepared_audio_info": self._clone_optional_data(self._prepared_audio_info),
            "validated_entity": self._validated_entity,
            "fibery_client": self._fibery_client,
            "entity_context": self._clone_optional_data(self._entity_context),
            "linked_transcript_text": self._linked_transcript_text,
            "transcript_mode": self._transcript_mode,
            "recording_mode": self._recording_mode,
            "summary_mode": self._summary_mode,
            "summary_language": self._summary_language,
        }

    def _restore_undo_snapshot(self, snapshot: dict) -> None:
        restored_session = snapshot.get("session")
        self._session = restored_session.clone() if restored_session else None
        self._prepared_audio_info = self._clone_optional_data(snapshot.get("prepared_audio_info"))
        self._validated_entity = snapshot.get("validated_entity")
        self._fibery_client = snapshot.get("fibery_client")
        self._entity_context = self._clone_optional_data(snapshot.get("entity_context"))
        self._linked_transcript_text = snapshot.get("linked_transcript_text", "")
        self._transcript_mode = snapshot.get("transcript_mode", "append")
        self._recording_mode = snapshot.get("recording_mode", "mic_only")
        self._summary_mode = snapshot.get("summary_mode", "append")
        self._summary_language = snapshot.get("summary_language", "en")
        self._reset_workflow_runtime_state()
        restored_state = snapshot.get("state", self.STATE_IDLE)
        # Undo snapshots only represent already-staged work. If the stored state says
        # "recording", we intentionally restore the session as idle because recreating
        # a live capture pipeline from a snapshot is not safe or supported.
        self.state = restored_state if restored_state != self.STATE_RECORDING else self.STATE_IDLE
        if self.state != self.STATE_RECORDING:
            self._resume_background_scanning()

    def _discard_current_workflow_locked(self) -> None:
        """Discard the current workflow state without preserving its audio."""
        current_session = self._session
        entity_for_lock = self._validated_entity if self._fibery_client else None

        self._decision_popup_active = False
        self._checkpoints = []
        self._recording_silence_start = None
        self._silence_checkpoint_added = False

        if self.state == self.STATE_RECORDING:
            if self._sleeping:
                self._sleeping = False
            else:
                try:
                    self.audio_capture.stop_capture()
                except Exception as e:
                    logger.warning("Error stopping capture during undo discard: %s", e)
                if self._mixer:
                    self._mixer.flush()
                    self._mixer = None
                if self._recorder:
                    seg_path = self._recorder.stop()
                    self._discard_paths([seg_path, self._recorder.compressed_path])
                    self._recorder = None
            self._release_recording_lock_async(entity_for_lock)

        self._discard_paths(self._recording_segments)
        self._discard_paths(self._segment_ogg_paths)
        self._cleanup_session_audio_files(current_session)
        self._session = None
        self._prepared_audio_info = None
        self._reset_workflow_runtime_state()
        self.state = self.STATE_IDLE
        self._resume_background_scanning()

    def get_session_snapshot(self) -> dict:
        """Return backend workflow state for frontend reconciliation."""
        with self._audio_lifecycle_lock:
            self._prune_expired_undo_snapshot()
            entity = self._validated_entity or (self._session.context.entity if self._session else None)
            return {
                "state": self.state,
                "prepared_audio": self._clone_optional_data(self._prepared_audio_info),
                "has_linked_meeting": bool(entity),
                "entity_name": getattr(entity, "entity_name", "") or "",
                "entity_database": getattr(entity, "database", "") or "",
                "undo_available": self._undo_snapshot is not None,
            }

    def reset_session_keep_meeting(self) -> None:
        """Clear workflow outputs while keeping the linked meeting context."""
        with self._audio_lifecycle_lock:
            if self.state in (self.STATE_RECORDING, self.STATE_PROCESSING):
                raise RuntimeError(f"Cannot reset workflow while in state: {self.state}")
            self._session_token += 1
            self._session = None
            self._prepared_audio_info = None
            self._reset_workflow_runtime_state()
            self.state = self.STATE_IDLE
            self._resume_background_scanning()
            logger.info("Workflow reset while keeping linked meeting (token=%d)", self._session_token)

    def stash_session_undo_snapshot(self, ttl_seconds: int = 15) -> dict:
        """Capture the current workflow state for temporary undo."""
        with self._audio_lifecycle_lock:
            self._prune_expired_undo_snapshot()
            ttl = max(1, int(ttl_seconds or 15))
            if self.state not in (self.STATE_PREPARED, self.STATE_COMPLETED) or not self._session:
                return {"stored": False, "undo_available": self._undo_snapshot is not None}
            self._undo_snapshot = self._build_undo_snapshot()
            self._undo_snapshot_expires_at = time.monotonic() + ttl
            return {"stored": True, "undo_available": True, "ttl_seconds": ttl}

    def undo_session_replace(self) -> dict:
        """Restore the last stashed workflow snapshot if it is still available."""
        with self._audio_lifecycle_lock:
            self._prune_expired_undo_snapshot()
            if not self._undo_snapshot:
                raise RuntimeError("No replacement session is available to undo.")
            snapshot = self._undo_snapshot
            self._clear_undo_snapshot()
            self._session_token += 1
            self._discard_current_workflow_locked()
            self._restore_undo_snapshot(snapshot)
            return self.get_session_snapshot()

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
        self._linked_transcript_text = ""
        self._session = None
        self._reset_workflow_runtime_state()
        self._transcript_mode = "append"
        self._recording_mode = "mic_only"
        self._summary_mode = "append"
        self._summary_language = "en"
        self._prepared_audio_info = None
        self._clear_undo_snapshot()
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

    def _release_recording_lock_for(self, entity, client=None) -> None:
        """Best-effort lock release for a specific entity."""
        if not entity:
            return

        created_client = None
        try:
            import socket

            if client is None:
                from integrations.fibery_client import FiberyClient

                api_token = get_key("fibery_api_token")
                if not api_token:
                    return
                created_client = FiberyClient(
                    api_token=api_token,
                    instance_url=FIBERY_INSTANCE_URL,
                )
                client = created_client

            current = client.get_recording_lock(entity)
            if current:
                name, host, _ = self._parse_lock(current)
                my_name = self._get_display_name()
                my_host = socket.gethostname()
                if name != my_name or (host and host != my_host):
                    logger.info("Lock belongs to %s@%s, not releasing", name, host)
                    return

            client.clear_recording_lock(entity)
        except Exception as e:
            logger.warning("Failed to release recording lock: %s", e)
        finally:
            if created_client:
                try:
                    created_client.close()
                except Exception:
                    logger.debug("Error closing temporary Fibery client", exc_info=True)

    def release_recording_lock(self) -> None:
        if not self._validated_entity or not self._fibery_client:
            return
        self._release_recording_lock_for(self._validated_entity, self._fibery_client)

    def _release_recording_lock_async(self, entity) -> None:
        """Release a recording lock off the UI-critical path."""
        if not entity:
            return

        def _worker() -> None:
            self._release_recording_lock_for(entity)

        threading.Thread(target=_worker, daemon=True).start()

    def deselect_meeting(self) -> None:
        """Clear the currently linked Fibery entity without closing the panel."""
        if self.state == self.STATE_PROCESSING:
            logger.info("Deselect blocked - processing is active")
            return
        if self._validated_entity:
            try:
                self.release_recording_lock()
            except Exception:
                logger.debug("Failed to release lock on deselect", exc_info=True)
        self._validated_entity = None
        self._entity_context = None
        self._linked_transcript_text = ""
        self._clear_undo_snapshot()
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

    def _get_local_transcript_text(self, session_results=None) -> str:
        """Return the current local transcript text, if any."""
        if not session_results:
            return ""

        cleaned_transcript = session_results.get_cleaned_transcript()
        if cleaned_transcript and cleaned_transcript.strip():
            return cleaned_transcript

        batch = session_results.get_batch_result()
        if batch and batch.get("utterances"):
            transcript_text = format_diarized_transcript(batch["utterances"])
            if transcript_text.strip():
                return transcript_text

        return ""

    def _get_summary_source_text(self, session: Optional[RecordingSession] = None) -> str:
        """Return the preferred transcript source for summarization."""
        active_session = session if session is not None else self._session
        session_results = active_session.results if active_session else None

        local_transcript = self._get_local_transcript_text(session_results)
        if local_transcript:
            return local_transcript

        if self._linked_transcript_text and self._linked_transcript_text.strip():
            return self._linked_transcript_text

        return ""

    # --- Level Monitoring ---

    def _allow_idle_loopback_capture(self) -> bool:
        """Whether idle monitoring/scanning may touch the loopback device."""
        return sys.platform != "win32"

    def _should_include_idle_loopback_capture(self, include_loopback: bool) -> bool:
        """Whether idle monitoring should open the selected loopback device."""
        return include_loopback or self._allow_idle_loopback_capture()

    def _reset_level_state(self, *, notify_js: bool = False) -> None:
        """Clear cached audio levels so the UI does not retain stale activity."""
        self._last_mic_level = 0.0
        self._last_raw_mic_level = 0.0
        self._last_sys_level = 0.0
        self._last_level_push = 0.0
        if notify_js:
            self._notify_js("window.updateAudioLevels && window.updateAudioLevels(0.0, 0.0)")

    def start_monitor(
        self,
        mic_index: Optional[int],
        loopback_index: Optional[int],
        include_loopback: bool = False,
    ) -> None:
        """Start audio level monitoring without recording."""
        with self._audio_lifecycle_lock:
            if self.state in (self.STATE_RECORDING, self.STATE_PROCESSING):
                return

            requested_selection = (mic_index, loopback_index)
            current_selection = (self._selected_mic_index, self._selected_sys_index)

            # No-op when idle monitoring is already running on the same devices.
            # This avoids needless stop/start churn and noisy warnings when UI
            # flows reassert the current selection.
            if (
                self.audio_capture.is_capturing()
                and requested_selection == current_selection
                and include_loopback == self._monitor_include_loopback
            ):
                self._selected_mic_index = mic_index
                self._selected_sys_index = loopback_index
                self._silence_counter_mic = 0
                self._silence_counter_sys = 0
                logger.debug(
                    "Level monitoring already active for current devices (mic=%s, loopback=%s, "
                    "include_loopback=%s)",
                    mic_index,
                    loopback_index,
                    include_loopback,
                )
                return

            # Stop any existing monitoring
            if self.audio_capture.is_capturing():
                logger.info(
                    "Level monitoring switching devices (old_mic=%s, old_loopback=%s, "
                    "new_mic=%s, new_loopback=%s, include_loopback=%s)",
                    self._selected_mic_index,
                    self._selected_sys_index,
                    mic_index,
                    loopback_index,
                    include_loopback,
                )
                self.audio_capture.stop_capture()

            mic_device = self._find_device(mic_index, is_loopback=False) if mic_index is not None else None
            loopback_device = self._find_device(loopback_index, is_loopback=True) if loopback_index is not None else None

            # Track selected devices for background scanner
            self._selected_mic_index = mic_index
            self._selected_sys_index = loopback_index
            self._monitor_include_loopback = include_loopback
            self._silence_counter_mic = 0
            self._silence_counter_sys = 0

            if loopback_device and not self._should_include_idle_loopback_capture(include_loopback):
                logger.info(
                    "Idle monitoring on Windows skips loopback capture for %s to avoid playback glitches",
                    loopback_device.name,
                )
                loopback_device = None
                self._last_sys_level = 0.0

            if not mic_device and not loopback_device:
                self._reset_level_state(notify_js=True)
                return

            # Skip noise suppression during idle monitoring to save CPU
            self._noise_suppressor = None

            self.audio_capture.start_capture(
                mic_device=mic_device,
                loopback_device=loopback_device,
                on_audio_chunk=lambda mic_pcm, sys_pcm: None,  # Discard audio data
                on_level_update=self._on_level_update,
                noise_suppressor=None,
            )
            logger.info(
                "Level monitoring started (mic=%s, loopback=%s, include_loopback=%s)",
                mic_device and mic_device.name,
                loopback_device and loopback_device.name,
                include_loopback,
            )

    def stop_monitor(self) -> None:
        """Stop audio level monitoring."""
        with self._audio_lifecycle_lock:
            if self.state == self.STATE_RECORDING:
                return
            if self.audio_capture.is_capturing():
                self.audio_capture.stop_capture()
                logger.info("Level monitoring stopped")
            self._monitor_include_loopback = False
            self._reset_level_state(notify_js=True)

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

    def check_for_updates(self) -> None:
        """Check GitHub for a newer release in the background.

        If an update is found, push a notification to the JS frontend.
        """
        from config.constants import APP_VERSION
        from utils.update_checker import check_for_update_async

        def _on_result(result):
            if result and not self._is_shutting_down:
                self._notify_js(
                    f"window.onUpdateAvailable && window.onUpdateAvailable("
                    f"{json.dumps(result)})"
                )

        check_for_update_async(APP_VERSION, _on_result)

    def start_background_scanning(self) -> None:
        """Start periodic background device scanning."""
        if not self._background_scanning_enabled:
            logger.debug("Background device scanning disabled")
            return
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
        thread = self._scan_thread
        if thread:
            self._scan_thread = None
            # Wait long enough for an in-progress scan to finish (~1.5s worst case)
            thread.join(timeout=5.0)
            logger.info("Background device scanning stopped")

    def _resume_background_scanning(self) -> None:
        """Ensure idle device scanning is running after recording/processing ends."""
        if (not self._background_scanning_enabled
                or self.state in (self.STATE_RECORDING, self.STATE_PROCESSING)):
            return
        self.start_background_scanning()

    _SILENCE_THRESHOLD = 0.005   # RMS below this = "silent"
    _SILENCE_TICKS_NEEDED = 2    # consecutive silent ticks before scanning

    _RECORDING_SILENCE_DURATION = 60.0    # seconds of total silence before decision popup
    _NO_SPEECH_SILENCE_DURATION = 300.0  # 5 min: mic noise but no speech before popup
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
                          and self._last_raw_mic_level < self._SILENCE_THRESHOLD)
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

            if scan_loopbacks and not self._allow_idle_loopback_capture():
                logger.debug("Skipping idle loopback background scan on Windows")
                scan_loopbacks = False

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

                # Skip devices currently being captured — opening a second
                # WASAPI stream on the same loopback device causes audio glitches.
                active_indices = set()
                if self._selected_mic_index is not None:
                    active_indices.add(self._selected_mic_index)
                if self._selected_sys_index is not None:
                    active_indices.add(self._selected_sys_index)

                report = scan_all_devices(
                    mic_devices=mic_devices,
                    loopback_devices=loopback_devices,
                    skip_indices=active_indices,
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
        with self._audio_lifecycle_lock:
            self._start_recording_locked(mic_index, loopback_index)

    def _start_recording_locked(
        self,
        mic_index: Optional[int],
        loopback_index: Optional[int],
    ) -> None:
        """Start audio capture and WAV recording."""
        if self.state != self.STATE_IDLE:
            raise RuntimeError("Cannot start recording from state: " + self.state)

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

        self._recording_channels = 2 if (mic_device is not None and loopback_device is not None) else 1
        self.stop_background_scanning()

        try:
            # Create noise suppressor for level monitoring (voice-aware silence detection)
            noise_suppressor = self._build_level_monitor_noise_suppressor()
            self._noise_suppressor = noise_suppressor

            # Set up audio mixer → feeds recorder
            self._mixer = AudioMixer(
                on_mixed_chunk=self._on_mixed_audio,
                has_mic=mic_device is not None,
                has_loopback=loopback_device is not None,
                output_channels=self._recording_channels,
            )

            # Start WAV recorder (raw WAV + processed OGG in parallel)
            self._start_recorder(self._recording_channels)

            # Start audio capture
            self.audio_capture.start_capture(
                mic_device=mic_device,
                loopback_device=loopback_device,
                on_audio_chunk=self._on_audio_chunk,
                on_level_update=self._on_level_update,
                noise_suppressor=noise_suppressor,
            )
        except Exception:
            self._resume_background_scanning()
            raise

        # Create session — captures entity snapshot frozen at recording start
        self._session = RecordingSession(SessionContext(
            entity=self._validated_entity,
            fibery_client=self._fibery_client,
            entity_context=self._entity_context,
        ))

        self._health_monitor.reset()
        self._last_lock_refresh = time.monotonic()
        self.state = self.STATE_RECORDING
        self._set_power_state(prevent_sleep=True)
        logger.info("Recording started (mic=%s, loopback=%s)",
                     mic_device and mic_device.name, loopback_device and loopback_device.name)

    def switch_sources(
        self,
        mic_index: Optional[int],
        loopback_index: Optional[int],
    ) -> None:
        """Switch audio sources while recording continues."""
        with self._audio_lifecycle_lock:
            self._switch_sources_locked(mic_index, loopback_index)

    def _switch_sources_locked(
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
        output_channels = self._recording_channels
        if output_channels is None:
            output_channels = old_mixer.channels if old_mixer else (2 if (mic_device and loopback_device) else 1)

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
            output_channels=output_channels,
        )

        # Start new capture -- on failure, try to restart with old devices
        ns = getattr(self, '_noise_suppressor', None)
        try:
            self.audio_capture.start_capture(
                mic_device=mic_device,
                loopback_device=loopback_device,
                on_audio_chunk=self._on_audio_chunk,
                on_level_update=self._on_level_update,
                noise_suppressor=ns,
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
                    output_channels=output_channels,
                )
                self.audio_capture.start_capture(
                    mic_device=old_mic,
                    loopback_device=old_loop,
                    on_audio_chunk=self._on_audio_chunk,
                    on_level_update=self._on_level_update,
                    noise_suppressor=ns,
                )
                logger.info("Rolled back to previous audio sources")
            except Exception:
                logger.error("Rollback also failed, recording continues without audio")
            raise RuntimeError(f"Failed to switch to new audio source: {e}") from e

        # Update tracked device indices
        self._selected_mic_index = mic_index
        self._selected_sys_index = loopback_index
        logger.info("Audio sources switched successfully")

    def stop_recording(self) -> Optional[dict]:
        """Stop capture and stage the recording for transcription."""
        with self._audio_lifecycle_lock:
            with self._stop_lock:
                if self.state != self.STATE_RECORDING:
                    return None
                return self._stop_recording_inner()

    def _stop_recording_inner(self) -> Optional[dict]:
        """Inner stop logic (caller must hold _stop_lock)."""
        self._set_power_state(prevent_sleep=False)
        # Clear decision popup state if active
        self._decision_popup_active = False
        self._checkpoints = []
        entity_for_lock = self._validated_entity if self._fibery_client else None

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

        self._release_recording_lock_async(entity_for_lock)

        # Merge segments if we have prior sleep-saved segments
        wav_path, compressed_path = self._finalize_segments()

        # Bake the file paths into the frozen session context.
        # Use current validated entity (user may have switched meetings during recording).
        if self._session and wav_path:
            info = self._set_prepared_session(
                entity=self._validated_entity or self._session.context.entity,
                fibery_client=self._fibery_client or self._session.context.fibery_client,
                entity_context=self._entity_context or self._session.context.entity_context,
                wav_path=str(wav_path),
                compressed_path=compressed_path or "",
                is_uploaded_file=False,
            )
            logger.info("Recording stopped, audio prepared for transcription")
            return info
        if wav_path:
            info = self._set_prepared_session(
                wav_path=str(wav_path),
                compressed_path=compressed_path or "",
                is_uploaded_file=False,
            )
            logger.info("Recording stopped, audio prepared without session snapshot")
            return info

        # No audio recorded; return to idle.
        self._prepared_audio_info = None
        self.state = self.STATE_IDLE
        self._resume_background_scanning()
        return None

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

    def _finalize_and_prepare(self) -> Optional[dict]:
        """Merge recorded segments and stage the result for later transcription."""
        try:
            wav_path, compressed_path = self._finalize_segments()
        except Exception as e:
            logger.error("Failed to merge segments: %s", e)
            wav_path, compressed_path = None, None

        if not wav_path:
            self._prepared_audio_info = None
            self.state = self.STATE_IDLE
            self._resume_background_scanning()
            return None

        if self._session:
            info = self._set_prepared_session(
                entity=self._validated_entity or self._session.context.entity,
                fibery_client=self._fibery_client or self._session.context.fibery_client,
                entity_context=self._entity_context or self._session.context.entity_context,
                wav_path=wav_path,
                compressed_path=compressed_path or "",
                is_uploaded_file=False,
            )
        else:
            info = self._set_prepared_session(
                wav_path=wav_path,
                compressed_path=compressed_path or "",
                is_uploaded_file=False,
            )

        logger.info("Finalized segments, audio prepared for transcription")
        return info

    # --- File Upload (Browse & Transcribe) ---

    SUPPORTED_AUDIO_EXTENSIONS = SUPPORTED_UPLOADED_AUDIO_EXTENSIONS

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
                "decoder_backend": "soundfile",
            }
        except ValueError:
            raise
        except Exception:
            pass  # Fall through to pydub for MP3/M4A/AAC

        # Try pydub for formats soundfile can't handle
        try:
            missing_tools = missing_ffmpeg_tools() if suffix in FFMPEG_BACKED_AUDIO_FORMATS else []
            if missing_tools:
                tools = " and ".join(missing_tools)
                verb = "are" if len(missing_tools) > 1 else "is"
                raise ValueError(
                    f"Cannot read {suffix.lstrip('.').upper()} files because {tools} {verb} not available. "
                    "MP3/M4A/AAC/WMA/WEBM uploads require ffmpeg support."
                )

            audio = load_audio_segment(path)
            duration = len(audio) / 1000.0
            if duration < 1.0:
                raise ValueError("Audio file is less than 1 second long.")
            return {
                "format": suffix.lstrip(".").upper(),
                "duration_seconds": duration,
                "sample_rate": audio.frame_rate,
                "channels": audio.channels,
                "size_bytes": size_bytes,
                "decoder_backend": "ffmpeg" if suffix in FFMPEG_BACKED_AUDIO_FORMATS else "pydub",
            }
        except ValueError:
            raise
        except ImportError:
            raise ValueError(
                f"Cannot read {suffix.lstrip('.').upper()} files because pydub is not installed."
            )
        except Exception as e:
            if suffix in FFMPEG_BACKED_AUDIO_FORMATS:
                raise ValueError(
                    f"Cannot decode {suffix.lstrip('.').upper()} audio: {e}. "
                    "Try exporting the file again or converting it to WAV."
                ) from e
            raise ValueError(f"Cannot read audio file: {e}") from e

    def prepare_uploaded_audio(self, file_path: str) -> dict:
        """Validate an uploaded audio file and stage it for transcription."""
        with self._audio_lifecycle_lock:
            return self._prepare_uploaded_audio_locked(file_path)

    def _prepare_uploaded_audio_locked(self, file_path: str) -> dict:
        """Validate an uploaded audio file and stage it for transcription."""
        if self.state in (self.STATE_RECORDING, self.STATE_PROCESSING):
            raise RuntimeError(f"Cannot upload while in state: {self.state}")

        path = Path(file_path)
        self._validate_audio_file(path)  # raises on failure
        path = self._copy_uploaded_file_to_recordings(path)

        if self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()

        suffix = path.suffix.lower()
        info = self._set_prepared_session(
            entity=self._validated_entity,
            fibery_client=self._fibery_client,
            entity_context=self._entity_context,
            wav_path=str(path),
            compressed_path=str(path) if suffix != ".wav" else "",
            is_uploaded_file=True,
        )
        logger.info("Uploaded audio prepared: %s", path.name)
        return info

    def start_transcription(self, options: Optional[TranscriptionOptions] = None) -> dict:
        """Start transcription for the currently staged audio file."""
        with self._audio_lifecycle_lock:
            return self._start_transcription_locked(options)

    def _start_transcription_locked(self, options: Optional[TranscriptionOptions] = None) -> dict:
        """Start transcription for the currently staged audio file."""
        if self.state not in (self.STATE_PREPARED, self.STATE_COMPLETED):
            raise RuntimeError(f"Cannot transcribe while in state: {self.state}")
        if not self._session or not self._session.context.wav_path:
            raise RuntimeError("No audio file is ready to transcribe.")

        if self.audio_capture.is_capturing():
            self.audio_capture.stop_capture()

        self._rename_placeholder_recording_for_selected_entity()

        options = options or TranscriptionOptions()
        effective_recording_mode, recording_mode_auto_corrected, recording_mode_reason = (
            self._normalize_recording_mode(options.recording_mode)
        )
        options = TranscriptionOptions(
            remove_echo=options.remove_echo,
            improve_with_context=options.improve_with_context,
            transcript_mode=options.transcript_mode,
            recording_mode=effective_recording_mode,
        )
        session = self._snapshot_session_for_transcription(self._session)
        results = session.results
        force_replace_send = results.get_transcript_sent()
        results.reset_transcription_outputs()

        self._session = session
        self._transcript_mode = "replace" if force_replace_send else options.transcript_mode
        self._recording_mode = effective_recording_mode
        self.state = self.STATE_PROCESSING
        logger.info(
            "Transcription started: %s (remove_echo=%s, improve_with_context=%s, mode=%s, recording_mode=%s)",
            Path(session.context.wav_path).name,
            options.remove_echo,
            options.improve_with_context,
            self._transcript_mode,
            effective_recording_mode,
        )

        self._batch_thread = threading.Thread(
            target=self._run_batch_processing,
            args=(session, options, force_replace_send),
            daemon=True,
        )
        self._batch_thread.start()
        return {
            "success": True,
            "transcript_mode": self._transcript_mode,
            "effective_recording_mode": effective_recording_mode,
            "recording_mode_auto_corrected": recording_mode_auto_corrected,
            "recording_mode_reason": recording_mode_reason,
            "force_replace_send": force_replace_send,
            "prepared_audio": dict(self._prepared_audio_info or {}),
        }

    def upload_and_transcribe(self, file_path: str) -> None:
        """Compatibility wrapper for older callers using the one-step API."""
        self.prepare_uploaded_audio(file_path)
        self.start_transcription(TranscriptionOptions())

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

    def _on_level_update(self, mic_level: float, sys_level: float, raw_mic_level: float = -1) -> None:
        """Called by audio capture with RMS levels. Non-blocking — safe from PortAudio callbacks.

        Puts the update into _level_queue; _level_dispatch_loop processes it on a background thread,
        keeping JS calls, health monitoring, and silence detection off the PortAudio callback thread.
        """
        try:
            self._level_queue.put_nowait((mic_level, sys_level, raw_mic_level))
        except queue.Full:
            pass  # drop rather than block the audio thread

    def _level_dispatch_loop(self) -> None:
        """Background thread: drain _level_queue and call _process_level_update."""
        while not self._is_shutting_down:
            try:
                mic_level, sys_level, raw_mic_level = self._level_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._process_level_update(mic_level, sys_level, raw_mic_level)
            except Exception:
                logger.debug("Level dispatch error", exc_info=True)

    def _process_level_update(self, mic_level: float, sys_level: float, raw_mic_level: float = -1) -> None:
        """Process a level update: push to JS, run health monitor, check silence."""
        if mic_level >= 0:
            self._last_mic_level = mic_level
        if raw_mic_level >= 0:
            self._last_raw_mic_level = raw_mic_level
        if sys_level >= 0:
            self._last_sys_level = sys_level

        # Throttle JS pushes to ~5/sec to avoid flooding WebView2 with evaluate_js calls.
        # Unthrottled, this fires ~20/sec (both audio sources × 10 callbacks/sec each).
        now = time.monotonic()
        if now - self._last_level_push >= self._LEVEL_PUSH_INTERVAL:
            self._last_level_push = now
            self._notify_js(
                f"window.updateAudioLevels({self._last_raw_mic_level:.4f}, {self._last_sys_level:.4f})"
            )

        # Audio health monitoring during recording — feed raw mic RMS so
        # noise suppression doesn't cause false "mic dead" reports
        if self.state == self.STATE_RECORDING:
            health_mic = raw_mic_level if raw_mic_level >= 0 else mic_level
            health = self._health_monitor.update(health_mic, sys_level)
            if health:
                self._notify_js(
                    f"window.updateAudioHealth && window.updateAudioHealth({json.dumps(health.to_dict())})"
                )
                for warning in self._health_monitor.check_warnings(health):
                    if warning.startswith("BOTH_DEAD:"):
                        # Both channels dead → auto-stop on a separate thread.
                        # stop_recording() calls stop_capture() → mic_stream.stop(), which
                        # deadlocks if called from the PortAudio callback or the loopback
                        # thread (can't stop a stream from within its own callback, and
                        # Thread.join() on the current thread raises RuntimeError).
                        msg = warning[len("BOTH_DEAD:"):]
                        self._notify_js(f"window.onHealthWarning && window.onHealthWarning({json.dumps(msg)})")
                        logger.warning("Both audio channels dead, triggering auto-stop")
                        threading.Thread(target=self._auto_stop_both_dead, daemon=True).start()
                        return
                    self._notify_js(f"window.onHealthWarning && window.onHealthWarning({json.dumps(warning)})")

        self._check_recording_silence()

    def _auto_stop_both_dead(self) -> None:
        """Stop recording when both audio channels die. Runs on a dedicated thread so that
        stop_capture() (which joins the loopback thread and stops the mic stream) is never
        called from within a PortAudio callback or the loopback thread itself."""
        info = self.stop_recording()
        self._notify_audio_prepared(info)

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

        # Tiered silence detection:
        #   sys audio active OR mic speech → reset (meeting active)
        #   mic noise but no speech/sys   → 5 min trigger
        #   both truly silent             → 60s trigger
        sys_has_audio = self._last_sys_level >= self._SILENCE_THRESHOLD
        mic_has_speech = self._last_mic_level >= SPEECH_THRESHOLD  # noise-suppressed
        mic_has_audio = self._last_raw_mic_level >= self._SILENCE_THRESHOLD

        if sys_has_audio or mic_has_speech:
            # Active meeting or speech — full reset
            self._recording_silence_start = None
            self._silence_checkpoint_added = False
        else:
            # Determine required silence duration based on mic state
            required = (self._NO_SPEECH_SILENCE_DURATION if mic_has_audio
                        else self._RECORDING_SILENCE_DURATION)

            if self._recording_silence_start is None:
                self._recording_silence_start = now
            elapsed = now - self._recording_silence_start

            if elapsed >= required:
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
                    logger.info("Silence detected for %.0fs (required %.0fs), showing decision popup",
                                elapsed, required)
                    threading.Thread(
                        target=self._save_milestone_segment,
                        args=("silence", True), daemon=True
                    ).start()

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
            self._start_recorder(self._recording_channels)
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
        info = self.stop_recording()
        self._notify_audio_prepared(info)

    def decision_end_at_checkpoint(self, checkpoint_index: int) -> None:
        """User chose to process up to a specific checkpoint."""
        with self._audio_lifecycle_lock:
            self._decision_end_at_checkpoint_locked(checkpoint_index)

    def _decision_end_at_checkpoint_locked(self, checkpoint_index: int) -> None:
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
            entity_for_lock = self._validated_entity if self._fibery_client else None

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

            self._release_recording_lock_async(entity_for_lock)

            # Discard segments after the checkpoint
            self._discard_segments_after(checkpoint.segment_index)

        # Process remaining segments
        info = self._finalize_and_prepare()
        self._notify_audio_prepared(info)

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
        """Called by power monitor when the system is going to sleep."""
        with self._audio_lifecycle_lock:
            self._on_system_sleep_locked()

    def _on_system_sleep_locked(self) -> None:
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
        self._set_power_state(prevent_sleep=False)

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
                info = self._finalize_and_prepare()
            except Exception as e2:
                logger.error("Finalize also failed: %s", e2)
                self.state = self.STATE_COMPLETED
                self._resume_background_scanning()
                info = None
            else:
                self._notify_audio_prepared(info)
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
        """Reinitialize audio pipeline and start a new recording segment."""
        with self._audio_lifecycle_lock:
            self._resume_recording_locked()

    def _resume_recording_locked(self) -> None:
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

        output_channels = self._recording_channels
        if output_channels is None:
            output_channels = 2 if (mic_device is not None and loopback_device is not None) else 1

        # Set up new mixer
        self._mixer = AudioMixer(
            on_mixed_chunk=self._on_mixed_audio,
            has_mic=mic_device is not None,
            has_loopback=loopback_device is not None,
            output_channels=output_channels,
        )

        # Start new recorder with the fixed session channel layout
        self._start_recorder(output_channels)

        # Start capture (reuse existing noise suppressor for level monitoring)
        self.audio_capture.start_capture(
            mic_device=mic_device,
            loopback_device=loopback_device,
            on_audio_chunk=self._on_audio_chunk,
            on_level_update=self._on_level_update,
            noise_suppressor=getattr(self, '_noise_suppressor', None),
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

        self._set_power_state(prevent_sleep=True)
        logger.info("Recording resumed (%.1f s recorded so far)", self._accumulated_recording_secs)

    # --- Batch Processing ---

    def _resolve_cleanup_audio_path(
        self,
        wav_path: str,
        compressed_path: Optional[str],
        batch_audio_path: str = "",
    ) -> str:
        """Pick a stable audio file to attach during transcript improvement."""
        if batch_audio_path:
            candidate = Path(batch_audio_path)
            if candidate.exists():
                return batch_audio_path

        if compressed_path:
            candidate = Path(compressed_path)
            if candidate.exists():
                return compressed_path

        if wav_path:
            wav_candidate = Path(wav_path)
            for ext in (".ogg", ".flac"):
                sidecar = wav_candidate.with_suffix(ext)
                if sidecar.exists():
                    return str(sidecar)
            if wav_candidate.exists():
                return wav_path

        return ""

    def _run_batch_processing(
        self,
        session: "RecordingSession",
        options: Optional[TranscriptionOptions] = None,
        force_replace_send: bool = False,
    ) -> None:
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
        options = options or TranscriptionOptions()
        token = self._session_token  # snapshot — stale if reset happens

        def _stale():
            return self._session_token != token

        # Upload audio to Fibery at transcription start (parallel with transcription)
        if self.settings.audio_storage == "fibery" and not results.get_audio_uploaded():
            threading.Thread(
                target=self._upload_audio_to_fibery,
                args=(wav_path, session, token),
                daemon=True,
            ).start()

        try:
            from transcription.batch import transcribe_with_diarization

            def on_progress(msg):
                logger.info("Batch: %s", msg)
                if not _stale():
                    self._notify_js(f"window.onProcessingProgress({json.dumps(msg)})")

            # Build keyterms prompt and diarization hints from frozen entity context
            keyterms_prompt = None
            speaker_hints = None
            entity_ctx = ctx.entity_context
            if not entity_ctx and ctx.entity and ctx.fibery_client:
                # Fallback: fetch if not captured at session start
                entity_ctx = self._fetch_entity_context()
            if entity_ctx:
                from integrations.context_builder import build_speaker_hints, build_keyterms_prompt
                keyterms_result = build_keyterms_prompt(entity_ctx)
                keyterms_prompt = keyterms_result.terms or None
                speaker_hints = build_speaker_hints(entity_ctx) or None
                skipped_summary = keyterms_result.format_skipped_reasons()
                if keyterms_prompt:
                    suffix = f" (filtered: {skipped_summary})" if skipped_summary else ""
                    logger.info(
                        "AssemblyAI automatic keyterms applied: %d phrases / %d words%s",
                        len(keyterms_result.terms),
                        keyterms_result.total_words,
                        suffix,
                    )
                else:
                    suffix = f" ({skipped_summary})" if skipped_summary else ""
                    logger.info(
                        "AssemblyAI automatic keyterms not applied: no high-confidence candidates survived filtering%s",
                        suffix,
                    )
            else:
                logger.info("AssemblyAI automatic keyterms not applied: no entity context available")

            # Post-processing settings from user preferences
            pp_settings = self._build_post_process_settings()

            result = transcribe_with_diarization(
                api_key=get_key("assemblyai_api_key"),
                audio_path=wav_path,
                on_progress=on_progress,
                compressed_path=compressed_path,
                keyterms_prompt=keyterms_prompt,
                speaker_hints=speaker_hints,
                remove_echo=options.remove_echo,
                recording_mode=options.recording_mode,
                post_process=pp_settings is not None,
                post_process_settings=pp_settings,
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
            if options.improve_with_context and gemini_key:
                try:
                    on_progress("Improving transcript...")
                    from integrations.gemini_client import cleanup_transcript
                    from integrations.context_builder import build_summary_context

                    meeting_context = build_summary_context(entity_ctx) if entity_ctx else ""
                    notes = ""
                    if ctx.entity and ctx.fibery_client:
                        try:
                            notes = ctx.fibery_client.get_entity_notes(ctx.entity) or ""
                        except Exception as exc:
                            logger.warning("Could not load Fibery notes for transcript improvement: %s", exc)

                    cleanup_audio = ""
                    if self.settings.audio_transcript_cleanup_enabled:
                        cleanup_audio = self._resolve_cleanup_audio_path(
                            wav_path,
                            compressed_path,
                            result.get("audio_path", ""),
                        )

                    cleaned = cleanup_transcript(
                        api_key=gemini_key,
                        transcript=raw_text,
                        notes=notes,
                        language=result.get("language", "en"),
                        meeting_context=meeting_context,
                        company_context=self.settings.company_context,
                        model=self.settings.gemini_model_cleanup,
                        model_fallback=self.settings.gemini_model_fallback,
                        audio_path=cleanup_audio,
                        on_progress=on_progress,
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
            self._resume_background_scanning()
            self._notify_js("window.onProcessingComplete()")

            # Auto-send transcript to Fibery using the frozen entity from session context
            if ctx.entity and ctx.fibery_client:
                threading.Thread(
                    target=self._auto_send_transcript,
                    args=(ctx.entity, ctx.fibery_client, session, token, force_replace_send),
                    daemon=True,
                ).start()

            uploaded_audio_path = result.get("audio_path", "")

            # Post-processing for uploaded (browsed) files:
            # Copy the actual uploaded audio to recordings_dir for local backup
            if ctx.is_uploaded_file and self.settings.save_recordings:
                self._copy_compressed_to_recordings(
                    wav_path,
                    compressed_path,
                    uploaded_audio_path,
                )

            # Clean up post-processed WAV (temporary artifact, not needed after upload)
            processed = Path(wav_path).parent / f"{Path(wav_path).stem}_processed.wav"
            if processed.exists():
                try:
                    processed.unlink()
                    logger.info("Cleaned up processed audio: %s", processed.name)
                except OSError as e:
                    logger.warning("Could not delete %s: %s", processed.name, e)

            if ctx.is_uploaded_file and not self.settings.save_recordings:
                self._cleanup_uploaded_audio_artifacts(wav_path, uploaded_audio_path)

            # Recorded sessions always keep the raw WAV locally.
            # When sidecar saving is off, remove generated compressed artifacts only.
            if not ctx.is_uploaded_file and not self.settings.save_recordings:
                self._cleanup_recorded_audio_sidecars(wav_path, uploaded_audio_path)

            logger.info("Batch processing complete: %d utterances", len(result["utterances"]))

        except Exception as e:
            logger.error("Batch processing failed: %s", e)
            if _stale():
                return
            # Mark as completed even on failure
            self.state = self.STATE_COMPLETED
            self._resume_background_scanning()
            self._notify_js(f"window.onBatchFailed({json.dumps({'message': _friendly_error(e), 'wav_path': wav_path or ''})})")
            self._notify_js(f"window.onError({json.dumps(_friendly_error(e))})")

    def _upload_audio_to_fibery(self, wav_path: str, session: "RecordingSession" = None, session_token: int = None) -> bool:
        """Upload the audio recording to the linked Fibery entity's Files field.

        Returns True on success, False on failure or skip.
        If session_token is given, UI callbacks are suppressed when the token
        no longer matches (session was reset).
        """
        # Use session context if available (prevents entity-swap bug)
        entity = session.context.entity if session else self._validated_entity
        client = session.context.fibery_client if session else self._fibery_client
        def _stale():
            return session_token is not None and self._session_token != session_token

        if not entity or not client:
            return False
        if _stale():
            logger.info("Audio upload skipped for stale session token")
            return False
        if not client.entity_supports_files(entity):
            logger.info(
                "Entity type %s does not support file attachments, skipping",
                entity.database,
            )
            return False
        results = session.results if session else None
        started_upload = False
        if results and not results.try_start_audio_upload():
            logger.info("Audio upload already in-flight, skipping")
            return False
        started_upload = bool(results)
        try:
            if _stale():
                if started_upload:
                    results.finish_audio_upload(success=False)
                logger.info("Audio upload aborted after session reset")
                return False
            if not _stale():
                self._notify_js(
                    'window.onProcessingProgress("Uploading audio to Fibery...")'
                )
            file_path = Path(wav_path)
            file_meta = client.upload_file(file_path)
            if _stale():
                if started_upload:
                    results.finish_audio_upload(success=False)
                logger.info("Audio attach skipped after session reset")
                return False
            file_id = file_meta["fibery/id"]
            client.attach_file_to_entity(entity, file_id)
            if results:
                results.finish_audio_upload(success=True)
            logger.info("Audio file uploaded to Fibery: %s", file_path.name)
            if not _stale():
                self._notify_js("window.onAudioUploadedToFibery()")

            return True

        except Exception as e:
            if results:
                results.finish_audio_upload(success=False)
            logger.error("Failed to upload audio to Fibery: %s", e)
            if not _stale():
                self._notify_js(
                    f"window.onAudioUploadError({json.dumps(_friendly_error(e))})"
                )
            return False

    def _copy_compressed_to_recordings(
        self,
        wav_path: str,
        compressed_path: str = None,
        actual_audio_path: str = None,
    ) -> None:
        """Copy the compressed audio file to recordings_dir for browsed files."""
        import shutil

        recordings_dir = self._get_recordings_dir()
        recordings_dir.mkdir(parents=True, exist_ok=True)

        source = Path(wav_path)
        # Check for compressed file created by batch.py (next to source)
        candidates = [
            Path(actual_audio_path) if actual_audio_path else None,
            Path(compressed_path) if compressed_path else None,
            source.with_suffix(".ogg"),
            source.with_suffix(".flac"),
            source.parent / f"{source.stem}_processed.ogg",
            source.parent / f"{source.stem}_processed.flac",
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

    def _cleanup_uploaded_audio_artifacts(self, wav_path: str, actual_audio_path: str = "") -> None:
        """Remove generated sidecar files for uploaded audio when local saving is off."""
        source = Path(wav_path)
        candidates: set[Path] = set()

        if source.suffix.lower() == ".wav":
            candidates.add(source.with_suffix(".ogg"))
            candidates.add(source.with_suffix(".flac"))

        for ext in (".wav", ".ogg", ".flac"):
            candidates.add(source.parent / f"{source.stem}_processed{ext}")
            candidates.add(source.parent / f"{source.stem}_mono_input{ext}")

        if actual_audio_path:
            actual = Path(actual_audio_path)
            if actual != source:
                candidates.add(actual)

        for candidate in candidates:
            if candidate == source or not candidate.exists():
                continue
            try:
                candidate.unlink()
                logger.info("Cleaned up uploaded-audio artifact: %s", candidate.name)
            except OSError as e:
                logger.warning("Could not delete %s: %s", candidate.name, e)

    def _cleanup_recorded_audio_sidecars(self, wav_path: str, actual_audio_path: str = "") -> None:
        """Remove generated OGG/FLAC sidecars for recorded audio while keeping the raw WAV."""
        source = Path(wav_path)
        candidates: set[Path] = set()

        for ext in (".wav", ".ogg", ".flac"):
            candidates.add(source.parent / f"{source.stem}_mono_input{ext}")
        for ext in (".ogg", ".flac"):
            candidates.add(source.with_suffix(ext))
            candidates.add(source.parent / f"{source.stem}_processed{ext}")

        if actual_audio_path:
            actual = Path(actual_audio_path)
            if actual != source and actual.suffix.lower() != ".wav":
                candidates.add(actual)

        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                candidate.unlink()
                logger.info("Cleaned up recorded-audio sidecar: %s", candidate.name)
            except OSError as e:
                logger.warning("Could not delete %s: %s", candidate.name, e)

    def _auto_send_pending_summary(
        self,
        entity,
        fibery_client,
        session: "RecordingSession" = None,
        session_token: int = None,
    ) -> None:
        """Send a cached generated summary to Fibery (background thread)."""
        def _stale():
            return session_token is not None and self._session_token != session_token

        result = self._send_pending_summary_to_target(
            entity,
            fibery_client,
            session=session,
            session_token=session_token,
        )
        if _stale():
            return
        if result.get("success"):
            self._notify_js("window.onPendingSummarySent()")
        elif not result.get("stale"):
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

    def _auto_send_transcript(
        self,
        entity,
        fibery_client,
        session: "RecordingSession" = None,
        session_token: int = None,
        force_replace: bool = False,
    ) -> None:
        """Send the current transcript to the Fibery Transcript field (background thread).

        Args:
            entity: The FiberyEntity to send to.
            fibery_client: The FiberyClient to use.
            session: If provided, reads transcript from session.results and marks sent.
            session_token: If provided, UI callbacks are suppressed when stale.
            force_replace: When True, always overwrite the existing Fibery transcript.
        """
        def _stale():
            return session_token is not None and self._session_token != session_token

        results = session.results if session else None
        started_send = False

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

        if _stale():
            logger.info("Transcript auto-send skipped for stale session token")
            return
        if results and not results.try_start_transcript_send():
            logger.info("Transcript send already in-flight, skipping")
            return
        started_send = bool(results)

        try:
            if _stale():
                if started_send:
                    results.finish_transcript_send(success=False)
                logger.info("Transcript auto-send aborted after session reset")
                return
            effective_append = not force_replace and self._transcript_mode == "append"
            if effective_append:
                fibery_client.update_transcript_only(entity, transcript_text, append=True)
            else:
                fibery_client.update_transcript_only(entity, transcript_text)
            if results:
                results.finish_transcript_send(success=True)
            if not _stale():
                self._notify_js("window.onTranscriptSentToFibery()")
            logger.info(
                "Transcript auto-sent to Fibery (mode=%s)",
                "append" if effective_append else "replace",
            )
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
        name = (name or "").strip()

        if not name:
            if meeting_type != "interview":
                return {'success': False, 'error': 'Meeting name is required'}

            # Market Interview names are formula-generated in Fibery, so send a
            # unique placeholder when the user intentionally leaves the field blank.
            import uuid as uuid_mod
            name = f"Interview placeholder {uuid_mod.uuid4().hex[:8]}"

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
            self._linked_transcript_text = ""

            logger.info('Created Fibery meeting: %s (%s)', name, meeting_type)

            result = {
                'success': True,
                'entity_name': entity.entity_name,
                'database': entity.database,
                'space': entity.space,
                'url': entity_url,
                'has_transcript': False,
                'transcript_text': '',
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
            try:
                self._linked_transcript_text = client.get_entity_transcript(entity) or ""
            except Exception as exc:
                logger.warning("Failed to fetch linked transcript: %s", exc)
                self._linked_transcript_text = ""

            has_notes = False
            if entity.database.lower() == "market interview":
                try:
                    notes_text = client.get_entity_notes(entity) or ""
                    has_notes = bool(notes_text.strip())
                except Exception as exc:
                    logger.warning("Failed to fetch linked notes: %s", exc)

            session = self._session
            session_results = session.results if session else None
            session_token = self._session_token

            # If transcript is already available (entity linked after recording), auto-send now
            session_batch = session_results.get_batch_result() if session_results else None
            if session_batch and session_batch.get("utterances"):
                threading.Thread(
                    target=self._auto_send_transcript,
                    args=(entity, client, session, session_token),
                    daemon=True,
                ).start()

            # If a summary was already generated without a link, send it now
            pending_summary = bool(
                session_results.get_generated_summary() if session_results else None
            )
            if pending_summary:
                threading.Thread(
                    target=self._auto_send_pending_summary,
                    args=(entity, client, session, session_token),
                    daemon=True,
                ).start()

            result = {
                "success": True,
                "entity_name": name,
                "database": entity.database,
                "space": entity.space,
                "pending_summary": pending_summary,
                "has_transcript": bool(self._linked_transcript_text.strip()),
                "transcript_text": self._linked_transcript_text,
                "has_notes": has_notes,
            }

            return result
        except Exception as e:
            logger.error("Fibery URL validation failed: %s", e)
            return {"success": False, "error": str(e)}

    def generate_summary(
        self,
        prompt_types: "list[str] | None" = None,
        custom_prompt: str = "",
        summary_style: str = "normal",
        summary_language: str = "",
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
        transcript_text = self._get_summary_source_text(session)
        if not transcript_text:
            return {"success": False, "error": "No transcript available"}
        normalized_summary_language = self._normalize_summary_language(summary_language or self._summary_language)
        self._summary_language = normalized_summary_language

        try:
            # Determine entity context for prompt (notes, interview vs meeting)
            # Use cached entity if available; otherwise use generic defaults
            notes = ""
            meeting_context = ""
            if entity and client:
                try:
                    notes = client.get_entity_notes(entity)
                except Exception:
                    pass
                # Build dynamic meeting context from entity
                entity_ctx = self._fetch_entity_context()
                if entity_ctx:
                    from integrations.context_builder import build_summary_context
                    meeting_context = build_summary_context(entity_ctx)

            logger.info(
                "Generating summary (style=%s, language=%s, has_entity=%s, prompt_types=%s)",
                summary_style,
                normalized_summary_language,
                bool(entity),
                prompt_types,
            )

            summary = summarize_transcript(
                api_key=get_key("gemini_api_key"),
                transcript=transcript_text,
                notes=notes,
                prompt_types=prompt_types or ["summarize"],
                custom_prompt=custom_prompt,
                summary_style=summary_style,
                summary_language=normalized_summary_language,
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
                    client.update_summary_only(entity, ai_summary=summary, append=(self._summary_mode == "append"))
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

    def _send_pending_summary_to_target(
        self,
        entity,
        client,
        session: "RecordingSession" = None,
        session_token: int = None,
    ) -> dict:
        """Send a cached generated summary to a specific Fibery target."""
        def _stale():
            return session_token is not None and self._session_token != session_token

        session_results = session.results if session else None
        summary = session_results.get_generated_summary() if session_results else None
        if not summary:
            return {"success": False, "error": "No summary available"}
        if not entity or not client:
            return {"success": False, "error": "No Fibery entity validated"}
        if _stale():
            logger.info("Pending summary send skipped for stale session token")
            return {"success": False, "stale": True, "error": "Session reset"}
        started_send = False
        if session_results and not session_results.try_start_summary_send():
            logger.info("Summary send already in-flight, skipping")
            return {"success": False, "error": "Summary send already in progress"}
        started_send = bool(session_results)
        try:
            if _stale():
                if started_send:
                    session_results.finish_summary_send(success=False)
                logger.info("Pending summary send aborted after session reset")
                return {"success": False, "stale": True, "error": "Session reset"}
            client.update_summary_only(entity, ai_summary=summary, append=(self._summary_mode == "append"))
            if session_results:
                session_results.finish_summary_send(success=True)
            logger.info("Pending summary sent to Fibery")
            return {"success": True}
        except Exception as e:
            if session_results:
                session_results.finish_summary_send(success=False)
            logger.error("Failed to send pending summary to Fibery: %s", e)
            return {"success": False, "error": _friendly_error(e)}

    def send_pending_summary_to_fibery(self) -> dict:
        """Send a previously generated summary to the validated Fibery entity."""
        # Capture locally to prevent TOCTOU
        session = self._session
        entity = self._validated_entity
        client = self._fibery_client
        return self._send_pending_summary_to_target(entity, client, session=session)

    def send_summary_to_fibery(
        self,
        fibery_url: str,
        prompt_types: "list[str] | None" = None,
        custom_prompt: str = "",
        summary_style: str = "normal",
        summary_language: str = "",
    ) -> dict:
        """Summarize transcript with Gemini and update the AI Summary field in Fibery."""

        from integrations.fibery_client import FiberyClient
        from integrations.gemini_client import summarize_transcript

        # Capture locally to prevent TOCTOU
        session = self._session
        session_results = session.results if session else None
        transcript_text = self._get_summary_source_text(session)
        if not transcript_text:
            return {"success": False, "error": "No transcript available"}
        normalized_summary_language = self._normalize_summary_language(summary_language or self._summary_language)
        self._summary_language = normalized_summary_language

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
                prompt_types=prompt_types or ["summarize"],
                custom_prompt=custom_prompt,
                summary_style=summary_style,
                summary_language=normalized_summary_language,
                model=self.settings.gemini_model,
                model_fallback=self.settings.gemini_model_fallback,
                company_context=self.settings.company_context,
                meeting_context=meeting_context,
            )

            if session_results:
                session_results.set_generated_summary(summary)
            client.update_summary_only(entity, ai_summary=summary, append=(self._summary_mode == "append"))
            if session_results:
                session_results.finish_summary_send(success=True)
            logger.info("AI Summary updated in Fibery")
            return {"success": True}

        except Exception as e:
            if session_results:
                session_results.finish_summary_send(success=False)
            logger.error("Fibery summarize workflow failed: %s", e)
            return {"success": False, "error": _friendly_error(e)}

    def check_problems_ready(self) -> dict:
        """Re-check Fibery for notes/transcript on the current Market Interview entity."""
        entity = self._validated_entity
        client = self._fibery_client
        if not entity or entity.database.lower() != "market interview":
            return {"success": False, "error": "No Market Interview linked"}
        if not client:
            return {"success": False, "error": "No Fibery client available"}
        has_notes = False
        has_transcript = False
        errors: list[str] = []
        try:
            transcript = client.get_entity_transcript(entity) or ""
            has_transcript = bool(transcript.strip())
        except Exception as exc:
            logger.warning("check_problems_ready: transcript fetch failed: %s", exc)
            errors.append(f"Could not fetch transcript from Fibery: {_friendly_error(exc)}")
        try:
            notes = client.get_entity_notes(entity) or ""
            has_notes = bool(notes.strip())
        except Exception as exc:
            logger.warning("check_problems_ready: notes fetch failed: %s", exc)
            errors.append(f"Could not fetch notes from Fibery: {_friendly_error(exc)}")
        if errors and not (has_notes or has_transcript):
            return {"success": False, "error": " ".join(errors)}
        return {"success": True, "has_notes": has_notes, "has_transcript": has_transcript}

    def generate_problems(self) -> dict:
        """Extract structured problems from a linked Market Interview and create them in Fibery.

        Reads Notes + Transcript from Fibery, sends to Gemini for structured problem extraction,
        then creates Market/Problem entities one-by-one. Transitions the interview to
        'Extract Problems' state after at least one problem is successfully created.
        """
        from integrations.fibery_client import FiberyClient
        from integrations.gemini_client import extract_problems

        # Capture locally to prevent TOCTOU (same pattern as generate_summary)
        entity = self._validated_entity
        client = self._fibery_client

        if not entity or entity.database.lower() != "market interview":
            return {"success": False, "error": "No Market Interview linked"}
        if not client:
            return {"success": False, "error": "No Fibery client available"}

        try:
            notes = ""
            transcript = ""
            try:
                notes = client.get_entity_notes(entity) or ""
            except Exception as e:
                logger.warning("Could not fetch notes for problem extraction: %s", e)
            try:
                transcript = client.get_entity_transcript(entity) or ""
            except Exception as e:
                logger.warning("Could not fetch transcript for problem extraction: %s", e)

            if not notes.strip() and not transcript.strip():
                return {"success": False, "error": "No notes or transcript found in Fibery"}

            segments = []
            try:
                segments = client.get_entity_segments(entity)
            except Exception as e:
                logger.warning("Could not fetch segments: %s", e)
            segment_hints = ", ".join(segments) if segments else ""

            interview_name = entity.entity_name
            try:
                interview_name = client.get_entity_name(entity)
            except Exception:
                pass

            meeting_context = ""
            try:
                entity_ctx = self._fetch_entity_context()
                if entity_ctx:
                    from integrations.context_builder import build_summary_context
                    meeting_context = build_summary_context(entity_ctx)
            except Exception as e:
                logger.warning("Could not build meeting context for problem extraction: %s", e)

            logger.info(
                "Generating problems for interview '%s' (notes=%d chars, transcript=%d chars)",
                interview_name, len(notes), len(transcript),
            )

            problems = extract_problems(
                api_key=get_key("gemini_api_key"),
                transcript=transcript,
                notes=notes,
                interview_name=interview_name,
                segment_hints=segment_hints,
                model=self.settings.gemini_model,
                model_fallback=self.settings.gemini_model_fallback,
                company_context=self.settings.company_context,
                meeting_context=meeting_context,
            )

            created_count = 0
            skipped_count = 0
            error_count = 0

            for problem_data in problems:
                struggle = (problem_data.get("struggle_with") or "").strip()
                if not struggle:
                    skipped_count += 1
                    logger.info("Skipping problem with empty struggle_with")
                    continue
                try:
                    client.create_problem_entity(entity, problem_data)
                    created_count += 1
                except Exception as e:
                    error_count += 1
                    logger.error("Failed to create problem entity '%.60s': %s", struggle, e)

            if created_count > 0:
                try:
                    client.set_interview_state(entity, FiberyClient._EXTRACT_PROBLEMS_STATE_ID)
                except Exception as e:
                    logger.warning("Failed to set interview state to Extract Problems: %s", e)

            logger.info(
                "Problem generation complete: created=%d, skipped=%d, errors=%d",
                created_count, skipped_count, error_count,
            )
            return {
                "success": True,
                "created_count": created_count,
                "skipped_count": skipped_count,
                "error_count": error_count,
            }

        except Exception as e:
            logger.error("Problem generation failed: %s", e)
            return {"success": False, "error": _friendly_error(e)}

    # --- UI Communication ---

    def _emergency_stop_recording(self) -> None:
        """Stop recording and save files without triggering batch processing."""
        with self._audio_lifecycle_lock:
            self._emergency_stop_recording_locked()

    def _emergency_stop_recording_locked(self) -> None:
        """Stop recording and save files without triggering batch processing.

        Used during shutdown to ensure WAV/OGG files are properly finalized
        (headers written, files closed) even though we won't transcribe.
        Handles active recording, sleeping, and decision popup states.
        """
        with self._stop_lock:
            if self.state != self.STATE_RECORDING:
                return
            self._set_power_state(prevent_sleep=False)
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
        with self._audio_lifecycle_lock:
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
