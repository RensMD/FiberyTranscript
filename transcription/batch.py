"""AssemblyAI batch transcription with speaker diarization."""

from __future__ import annotations

import logging
import re
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

from audio.file_formats import load_audio_segment

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds

_COMPRESS_CHUNK_FRAMES = 65536  # ~4 s at 16 kHz â€” keeps memory usage low
_TEXT_TOKEN_RE = re.compile(r"[a-z0-9']+")
_ECHO_MAX_START_DELTA_MS = 250
_ECHO_MIN_OVERLAP_RATIO = 0.65
_ECHO_MIN_OVERLAP_MS = 350
_ECHO_MIN_TEXT_SIMILARITY = 0.96
_ECHO_MIN_CONFIDENCE_GAP = 0.12
_ECHO_MAX_MIC_DURATION_MS = 2200
_ECHO_MAX_MIC_WORDS = 12
_MONO_INPUT_SUFFIX = "_mono_input"


def _compress_audio(wav_path: str) -> str:
    """Compress WAV to a smaller format for faster upload."""
    try:
        import soundfile as sf

        # Try OGG Vorbis first â€” much smaller than FLAC for speech audio
        try:
            ogg_path = str(Path(wav_path).with_suffix(".ogg"))
            with sf.SoundFile(wav_path, "r") as src:
                with sf.SoundFile(
                    ogg_path,
                    "w",
                    samplerate=src.samplerate,
                    channels=src.channels,
                    format="OGG",
                    subtype="VORBIS",
                ) as dst:
                    while True:
                        chunk = src.read(_COMPRESS_CHUNK_FRAMES)
                        if len(chunk) == 0:
                            break
                        dst.write(chunk)
            wav_size = Path(wav_path).stat().st_size
            ogg_size = Path(ogg_path).stat().st_size
            reduction = (1 - ogg_size / wav_size) * 100
            logger.info(
                "Compressed %s -> OGG Vorbis (%.0f%% smaller: %.1f MB -> %.1f MB)",
                Path(wav_path).name,
                reduction,
                wav_size / 1e6,
                ogg_size / 1e6,
            )
            return ogg_path
        except Exception as e:
            logger.debug("OGG Vorbis compression failed, trying FLAC: %s", e)

        try:
            flac_path = str(Path(wav_path).with_suffix(".flac"))
            with sf.SoundFile(wav_path, "r") as src:
                with sf.SoundFile(
                    flac_path,
                    "w",
                    samplerate=src.samplerate,
                    channels=src.channels,
                    format="FLAC",
                ) as dst:
                    while True:
                        chunk = src.read(_COMPRESS_CHUNK_FRAMES)
                        if len(chunk) == 0:
                            break
                        dst.write(chunk)
            wav_size = Path(wav_path).stat().st_size
            flac_size = Path(flac_path).stat().st_size
            reduction = (1 - flac_size / wav_size) * 100
            logger.info(
                "Compressed %s -> FLAC (%.0f%% smaller: %.1f MB -> %.1f MB)",
                Path(wav_path).name,
                reduction,
                wav_size / 1e6,
                flac_size / 1e6,
            )
            return flac_path
        except Exception as e:
            logger.debug("FLAC compression also failed: %s", e)
            return wav_path

    except ImportError:
        logger.debug("soundfile not installed, uploading uncompressed audio")
        return wav_path
    except Exception as e:
        logger.warning("Audio compression failed, using source audio: %s", e)
        return wav_path


def _read_audio_info(audio_path: str) -> dict:
    """Read channel and sample-rate metadata with a soundfile/pydub fallback."""
    try:
        import soundfile as sf

        info = sf.info(audio_path)
        return {
            "channels": int(info.channels),
            "sample_rate": int(getattr(info, "samplerate", 16000) or 16000),
            "duration_seconds": float(getattr(info, "duration", 0.0) or 0.0),
        }
    except Exception:
        logger.debug("soundfile metadata read failed for %s", audio_path, exc_info=True)

    try:
        audio = load_audio_segment(audio_path)
        return {
            "channels": int(audio.channels),
            "sample_rate": int(audio.frame_rate),
            "duration_seconds": len(audio) / 1000.0,
        }
    except Exception as exc:
        raise RuntimeError(f"Could not inspect audio metadata for {audio_path}: {exc}") from exc


def _build_mono_input_path(audio_path: str) -> Path:
    source = Path(audio_path)
    return source.parent / f"{source.stem}{_MONO_INPUT_SUFFIX}.wav"


def _downmix_to_mono_wav(audio_path: str) -> str:
    """Downmix any multi-channel file to a mono WAV for single-channel transcription."""
    output_path = _build_mono_input_path(audio_path)

    try:
        import soundfile as sf

        with sf.SoundFile(audio_path, "r") as src:
            with sf.SoundFile(
                str(output_path),
                "w",
                samplerate=src.samplerate,
                channels=1,
                format="WAV",
                subtype="PCM_16",
            ) as dst:
                while True:
                    chunk = src.read(_COMPRESS_CHUNK_FRAMES, dtype="float32", always_2d=True)
                    if len(chunk) == 0:
                        break
                    dst.write(chunk.mean(axis=1))
        return str(output_path)
    except Exception:
        logger.debug("soundfile mono downmix failed for %s", audio_path, exc_info=True)

    try:
        audio = load_audio_segment(audio_path)
        audio.set_channels(1).export(str(output_path), format="wav")
        return str(output_path)
    except Exception as exc:
        raise RuntimeError(f"Could not downmix audio to mono for transcription: {exc}") from exc


def _prepare_upload_path(
    audio_path: str,
    compressed_path: Optional[str],
    on_progress: Optional[Callable[[str], None]],
    label: str = "audio",
) -> str:
    """Return the file path that should be uploaded to AssemblyAI."""
    if compressed_path and Path(compressed_path).exists():
        upload_path = compressed_path
        source_size = Path(audio_path).stat().st_size if Path(audio_path).exists() else 0
        upload_size = Path(upload_path).stat().st_size
        reduction = (1 - upload_size / source_size) * 100 if source_size else 0
        logger.info(
            "Using pre-compressed %s (%.0f%% smaller: %.1f MB -> %.1f MB)",
            label,
            reduction,
            source_size / 1e6 if source_size else 0,
            upload_size / 1e6,
        )
    else:
        if on_progress:
            on_progress(f"Compressing {label}...")
        upload_path = _compress_audio(audio_path)

    upload_size = Path(upload_path).stat().st_size
    if on_progress:
        on_progress(f"Uploading {label} ({upload_size / 1e6:.1f} MB)...")
    return upload_path


def _apply_speaker_hints(
    config_kwargs: dict,
    speaker_hints: Optional[dict],
    *,
    multichannel: bool,
) -> None:
    """Add optional speaker-count and speaker-identification hints."""
    if not speaker_hints:
        return

    if speaker_hints.get("speakers_expected") is not None:
        config_kwargs["speakers_expected"] = speaker_hints["speakers_expected"]
    elif speaker_hints.get("speaker_options"):
        config_kwargs["speaker_options"] = speaker_hints["speaker_options"]

    known_speakers = speaker_hints.get("speaker_identification") or []
    if multichannel and known_speakers:
        logger.info(
            "Skipping speaker identification hints for multichannel transcription; "
            "AssemblyAI does not support speaker_identification with multichannel diarization"
        )
        return

    if known_speakers:
        config_kwargs["speech_understanding"] = {
            "request": {
                "speaker_identification": {
                    "speaker_type": "name",
                    "known_values": known_speakers,
                }
            }
        }


def _build_config_kwargs(
    *,
    keyterms_prompt: Optional[list[str]],
    speaker_hints: Optional[dict],
    multichannel: bool,
) -> dict:
    config_kwargs = {
        "speaker_labels": True,
        "speech_models": ["universal-3-pro", "universal-2"],
        "language_detection": True,
    }
    if multichannel:
        config_kwargs["multichannel"] = True
    if keyterms_prompt:
        config_kwargs["keyterms_prompt"] = keyterms_prompt
        logger.info(
            "Keyterms prompt: %d phrases / %d words",
            len(keyterms_prompt),
            sum(len(term.split()) for term in keyterms_prompt),
        )
    _apply_speaker_hints(config_kwargs, speaker_hints, multichannel=multichannel)
    return config_kwargs


def _resolve_speech_model_used(transcript, config_kwargs: dict) -> str:
    """Return the best available model identifier for logs/debugging."""
    resolved = getattr(transcript, "speech_model", "") or ""
    if resolved:
        return resolved

    requested = list(config_kwargs.get("speech_models") or [])
    if len(requested) == 1:
        return requested[0]
    if len(requested) > 1:
        return "auto"
    return ""


def _upload_and_transcribe(
    aai,
    transcriber,
    upload_path: str,
    config_kwargs: dict,
    on_progress: Optional[Callable[[str], None]],
    transcribe_message: str,
):
    """Upload audio once and run the transcription request."""
    config = aai.TranscriptionConfig(**config_kwargs)
    upload_url = None
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1 and on_progress:
                on_progress(f"Retrying upload (attempt {attempt}/{MAX_RETRIES})...")
            upload_url = transcriber.upload_file(upload_path)
            logger.info("Audio uploaded: %s", upload_url)
            break
        except Exception as exc:
            last_error = exc
            logger.warning("Upload attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    else:
        raise last_error

    if on_progress:
        on_progress(transcribe_message)

    transcript = transcriber.transcribe(upload_url, config=config)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"Transcription failed: {transcript.error}")
    return transcript


def _extract_utterances(transcript, *, channel: Optional[int] = None) -> list[dict]:
    """Normalize AssemblyAI utterances into the app's internal dict format."""
    utterances: list[dict] = []
    for utterance in transcript.utterances or []:
        entry = {
            "speaker": utterance.speaker,
            "text": utterance.text,
            "start": utterance.start,
            "end": utterance.end,
            "confidence": getattr(utterance, "confidence", None),
        }
        raw_channel = getattr(utterance, "channel", None)
        effective_channel = channel if channel is not None else raw_channel
        if effective_channel is not None:
            entry["channel"] = effective_channel
        utterances.append(entry)
    return utterances


def _build_echo_mode_speaker_hints(speaker_hints: Optional[dict]) -> Optional[dict]:
    """Relax exact speaker counts when transcribing stereo channels separately."""
    if not speaker_hints:
        return None

    hints: dict = {}
    exact_count = speaker_hints.get("speakers_expected")
    if exact_count is not None:
        hints["speaker_options"] = {
            "min_speakers_expected": 1,
            "max_speakers_expected": max(1, exact_count),
            "use_two_stage_clustering": True,
        }
    elif speaker_hints.get("speaker_options"):
        hints["speaker_options"] = dict(speaker_hints["speaker_options"])

    if speaker_hints.get("speaker_identification"):
        hints["speaker_identification"] = list(speaker_hints["speaker_identification"])

    return hints or None


def _split_stereo_to_mono_wavs(audio_path: str, output_dir: str) -> list[str]:
    """Split a stereo file into one mono WAV per channel."""
    try:
        import soundfile as sf

        output_paths = [str(Path(output_dir) / "channel_0.wav"), str(Path(output_dir) / "channel_1.wav")]
        with sf.SoundFile(audio_path, "r") as src:
            if src.channels < 2:
                raise ValueError("Audio is not stereo.")
            with sf.SoundFile(
                output_paths[0],
                "w",
                samplerate=src.samplerate,
                channels=1,
                format="WAV",
                subtype="PCM_16",
            ) as ch0, sf.SoundFile(
                output_paths[1],
                "w",
                samplerate=src.samplerate,
                channels=1,
                format="WAV",
                subtype="PCM_16",
            ) as ch1:
                while True:
                    chunk = src.read(_COMPRESS_CHUNK_FRAMES, always_2d=True)
                    if len(chunk) == 0:
                        break
                    ch0.write(chunk[:, 0])
                    ch1.write(chunk[:, 1])
        return output_paths
    except Exception:
        logger.debug("soundfile stereo split failed for %s", audio_path, exc_info=True)

    try:
        output_paths = []
        audio = load_audio_segment(audio_path)
        mono_channels = audio.split_to_mono()
        if len(mono_channels) < 2:
            raise ValueError("Audio is not stereo.")
        for index, mono in enumerate(mono_channels[:2]):
            output_path = str(Path(output_dir) / f"channel_{index}.wav")
            mono.export(output_path, format="wav")
            output_paths.append(output_path)
        return output_paths
    except Exception as exc:
        raise RuntimeError(f"Could not split stereo audio for echo removal: {exc}") from exc


def _normalize_text(text: str) -> str:
    return " ".join(_TEXT_TOKEN_RE.findall((text or "").lower()))


def _token_overlap(a: str, b: str) -> float:
    tokens_a = set(_normalize_text(a).split())
    tokens_b = set(_normalize_text(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / max(1, min(len(tokens_a), len(tokens_b)))


def _utterance_overlap_ms(a: dict, b: dict) -> int:
    return max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _is_probable_echo(mic_utterance: dict, loopback_utterance: dict) -> bool:
    """Return True when a mic utterance looks like loopback echo."""
    if mic_utterance.get("channel") != 0 or loopback_utterance.get("channel") != 1:
        return False

    mic_duration = max(0, mic_utterance["end"] - mic_utterance["start"])
    if mic_duration == 0 or mic_duration > _ECHO_MAX_MIC_DURATION_MS:
        return False

    start_delta = abs(mic_utterance["start"] - loopback_utterance["start"])
    if start_delta > _ECHO_MAX_START_DELTA_MS:
        return False

    overlap_ms = _utterance_overlap_ms(mic_utterance, loopback_utterance)
    if overlap_ms < _ECHO_MIN_OVERLAP_MS:
        return False

    loopback_duration = max(0, loopback_utterance["end"] - loopback_utterance["start"])
    overlap_baseline = min(mic_duration, loopback_duration)
    if overlap_baseline <= 0:
        return False
    if (overlap_ms / overlap_baseline) < _ECHO_MIN_OVERLAP_RATIO:
        return False

    mic_text = _normalize_text(mic_utterance.get("text", ""))
    loop_text = _normalize_text(loopback_utterance.get("text", ""))
    if not mic_text or not loop_text:
        return False

    mic_words = mic_text.split()
    loop_words = loop_text.split()
    if len(mic_words) > _ECHO_MAX_MIC_WORDS:
        return False

    similarity = SequenceMatcher(None, mic_text, loop_text).ratio()
    token_overlap = _token_overlap(mic_text, loop_text)
    near_exact_text_match = (
        mic_text == loop_text
        or (
            similarity >= _ECHO_MIN_TEXT_SIMILARITY
            and token_overlap >= 1.0
            and abs(len(mic_words) - len(loop_words)) <= 1
        )
    )
    if not near_exact_text_match:
        return False

    mic_conf = mic_utterance.get("confidence")
    loop_conf = loopback_utterance.get("confidence")
    if mic_conf is None or loop_conf is None:
        return False

    mic_conf = float(mic_conf)
    loop_conf = float(loop_conf)
    return (loop_conf - mic_conf) >= _ECHO_MIN_CONFIDENCE_GAP


def _suppress_echo_duplicates(utterances: list[dict]) -> list[dict]:
    """Drop mic-channel utterances that duplicate nearby loopback speech."""
    loopback_utterances = [u for u in utterances if u.get("channel") == 1]
    filtered: list[dict] = []

    for utterance in utterances:
        if utterance.get("channel") != 0:
            filtered.append(utterance)
            continue

        should_drop = any(
            _is_probable_echo(utterance, candidate)
            for candidate in loopback_utterances
        )
        if not should_drop:
            filtered.append(utterance)

    return sorted(
        filtered,
        key=lambda item: (
            item.get("start", 0),
            0 if item.get("channel") == 1 else 1,
            item.get("end", 0),
        ),
    )


def transcribe_with_diarization(
    api_key: str,
    audio_path: str,
    on_progress: Optional[Callable[[str], None]] = None,
    compressed_path: Optional[str] = None,
    keyterms_prompt: Optional[list[str]] = None,
    speaker_hints: Optional[dict] = None,
    remove_echo: bool = False,
    recording_mode: str = "mic_and_speakers",
    post_process: bool = True,
    post_process_settings: Optional[dict] = None,
) -> dict:
    """Transcribe audio file with speaker diarization."""
    import assemblyai as aai

    aai.settings.api_key = api_key

    if post_process:
        try:
            from audio.post_processor import PostProcessor

            pp_kwargs = post_process_settings or {}
            processor = PostProcessor(**pp_kwargs)
            original_audio_path = Path(audio_path)
            processed_path = Path(processor.process(original_audio_path, on_progress=on_progress))
            if processed_path.resolve(strict=False) != original_audio_path.resolve(strict=False):
                audio_path = str(processed_path)
                compressed_path = None
            else:
                logger.info("Post-processing made no file changes; keeping existing compressed fallback")
        except Exception:
            logger.warning("Post-processing failed, falling back to existing compressed audio", exc_info=True)

    audio_info = _read_audio_info(audio_path)
    channel_count = audio_info.get("channels", 1)
    requested_recording_mode = (
        recording_mode if recording_mode in ("mic_only", "mic_and_speakers") else "mic_and_speakers"
    )
    effective_recording_mode = (
        "mic_and_speakers" if requested_recording_mode == "mic_and_speakers" and channel_count >= 2 else "mic_only"
    )
    is_multichannel = effective_recording_mode == "mic_and_speakers"

    if effective_recording_mode == "mic_only" and channel_count >= 2:
        if on_progress:
            on_progress("Preparing mono transcription input...")
        audio_path = _downmix_to_mono_wav(audio_path)
        compressed_path = None
        audio_info = _read_audio_info(audio_path)
        channel_count = audio_info.get("channels", 1)
        logger.info("Downmixed %s to mono for mic-only transcription", Path(audio_path).name)

    if remove_echo and is_multichannel:
        if on_progress:
            on_progress("Preparing echo-aware transcription...")

        transcriber = aai.Transcriber()
        mono_speaker_hints = _build_echo_mode_speaker_hints(speaker_hints)
        all_utterances: list[dict] = []
        languages: list[str] = []

        with tempfile.TemporaryDirectory(prefix="fibery_echo_") as temp_dir:
            mono_paths = _split_stereo_to_mono_wavs(audio_path, temp_dir)
            speech_model_used = ""

            for channel_index, mono_path in enumerate(mono_paths):
                channel_label = "microphone channel" if channel_index == 0 else "speaker channel"
                upload_path = _prepare_upload_path(mono_path, None, on_progress, channel_label)
                config_kwargs = _build_config_kwargs(
                    keyterms_prompt=keyterms_prompt,
                    speaker_hints=mono_speaker_hints,
                    multichannel=False,
                )
                transcript = _upload_and_transcribe(
                    aai,
                    transcriber,
                    upload_path,
                    config_kwargs,
                    on_progress,
                    f"Transcribing {channel_label}...",
                )
                speech_model_used = speech_model_used or _resolve_speech_model_used(transcript, config_kwargs)
                all_utterances.extend(_extract_utterances(transcript, channel=channel_index))
                languages.append(getattr(transcript, "language_code", "unknown"))

        merged_utterances = sorted(
            all_utterances,
            key=lambda item: (
                item.get("start", 0),
                0 if item.get("channel") == 1 else 1,
                item.get("end", 0),
            ),
        )
        deduped_utterances = _suppress_echo_duplicates(merged_utterances)

        if on_progress:
            on_progress("Processing complete!")

        stable_audio_path = (
            compressed_path
            if compressed_path and Path(compressed_path).exists()
            else audio_path
        )
        return {
            "utterances": deduped_utterances,
            "full_text": " ".join(
                utterance.get("text", "").strip()
                for utterance in deduped_utterances
                if utterance.get("text", "").strip()
            ),
            "language": next((lang for lang in languages if lang and lang != "unknown"), "unknown"),
            "speech_model_used": speech_model_used,
            "audio_path": stable_audio_path,
            "effective_recording_mode": effective_recording_mode,
        }

    upload_path = _prepare_upload_path(audio_path, compressed_path, on_progress)
    config_kwargs = _build_config_kwargs(
        keyterms_prompt=keyterms_prompt,
        speaker_hints=speaker_hints,
        multichannel=is_multichannel,
    )
    if is_multichannel:
        logger.info("Using multichannel transcription (stereo)")

    transcriber = aai.Transcriber()
    transcript = _upload_and_transcribe(
        aai,
        transcriber,
        upload_path,
        config_kwargs,
        on_progress,
        "Transcribing...",
    )

    if on_progress:
        on_progress("Processing complete!")

    return {
        "utterances": _extract_utterances(transcript),
        "full_text": transcript.text or "",
        "language": getattr(transcript, "language_code", "unknown"),
        "speech_model_used": _resolve_speech_model_used(transcript, config_kwargs),
        "audio_path": upload_path,
        "effective_recording_mode": effective_recording_mode,
    }
