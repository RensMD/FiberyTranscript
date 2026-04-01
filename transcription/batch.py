"""AssemblyAI batch transcription with speaker diarization."""

import logging
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds


_COMPRESS_CHUNK_FRAMES = 65536  # ~4 s at 16 kHz — keeps memory usage low


def _compress_audio(wav_path: str) -> str:
    """Compress WAV to a smaller format for faster upload.

    Uses streaming I/O to avoid loading the entire file into memory
    (large WAVs can be several GB). Tries OGG Vorbis first, then FLAC,
    then falls back to uncompressed WAV.
    """
    try:
        import soundfile as sf

        # Try OGG Vorbis first — much smaller than FLAC for speech audio
        try:
            ogg_path = str(Path(wav_path).with_suffix(".ogg"))
            with sf.SoundFile(wav_path, "r") as src:
                with sf.SoundFile(
                    ogg_path, "w",
                    samplerate=src.samplerate,
                    channels=src.channels,
                    format="OGG", subtype="VORBIS",
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
                "Compressed %s → OGG Vorbis (%.0f%% smaller: %.1f MB → %.1f MB)",
                Path(wav_path).name, reduction, wav_size / 1e6, ogg_size / 1e6,
            )
            return ogg_path
        except Exception as e:
            logger.debug("OGG Vorbis compression failed, trying FLAC: %s", e)

        # Fall back to FLAC (streaming)
        try:
            flac_path = str(Path(wav_path).with_suffix(".flac"))
            with sf.SoundFile(wav_path, "r") as src:
                with sf.SoundFile(
                    flac_path, "w",
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
                "Compressed %s → FLAC (%.0f%% smaller: %.1f MB → %.1f MB)",
                Path(wav_path).name, reduction, wav_size / 1e6, flac_size / 1e6,
            )
            return flac_path
        except Exception as e:
            logger.debug("FLAC compression also failed: %s", e)
            return wav_path

    except ImportError:
        logger.debug("soundfile not installed, uploading uncompressed WAV")
        return wav_path
    except Exception as e:
        logger.warning("Audio compression failed, using WAV: %s", e)
        return wav_path


def transcribe_with_diarization(
    api_key: str,
    audio_path: str,
    on_progress: Optional[Callable[[str], None]] = None,
    compressed_path: Optional[str] = None,
    word_boost: Optional[list[str]] = None,
    post_process: bool = True,
    post_process_settings: Optional[dict] = None,
) -> dict:
    """Transcribe audio file with speaker diarization.

    Args:
        api_key: AssemblyAI API key.
        audio_path: Path to WAV file.
        on_progress: Optional callback for progress updates.
        compressed_path: Pre-compressed file from streaming recording.
            Skips the compression step if provided and valid.
        post_process: Run post-processing pipeline before upload.
        post_process_settings: Optional stage toggles with keys
            echo_cancel, noise_suppress, agc, normalize.

    Returns:
        Dict with 'utterances' (list of speaker-labeled segments),
        'full_text', and 'language'.
    """
    import assemblyai as aai

    aai.settings.api_key = api_key

    # Post-processing pipeline (echo cancellation, denoise, AGC, normalize).
    # On success, use the processed WAV for upload (force re-compression).
    # On failure, fall back to the parallel OGG (has denoise+AGC from recording).
    if post_process:
        try:
            from audio.post_processor import PostProcessor
            pp_kwargs = post_process_settings or {}
            processor = PostProcessor(**pp_kwargs)
            original_audio_path = Path(audio_path)
            processed_path = Path(processor.process(original_audio_path, on_progress=on_progress))
            if processed_path.resolve(strict=False) != original_audio_path.resolve(strict=False):
                audio_path = str(processed_path)
                compressed_path = None  # Force re-compression of fully processed audio
            else:
                logger.info("Post-processing made no file changes; keeping existing compressed fallback")
        except Exception:
            logger.warning("Post-processing failed, falling back to parallel OGG", exc_info=True)
            # compressed_path (parallel OGG with denoise+AGC) is the fallback

    # Detect channel count for multichannel transcription
    is_multichannel = False
    try:
        import soundfile as sf
        info = sf.info(audio_path)
        is_multichannel = info.channels >= 2
    except Exception:
        logger.debug("Could not detect channel count", exc_info=True)

    # Use pre-compressed file if available, otherwise compress now
    if compressed_path and Path(compressed_path).exists():
        upload_path = compressed_path
        wav_size = Path(audio_path).stat().st_size
        comp_size = Path(compressed_path).stat().st_size
        reduction = (1 - comp_size / wav_size) * 100 if wav_size else 0
        logger.info(
            "Using pre-compressed audio (%.0f%% smaller: %.1f MB → %.1f MB)",
            reduction, wav_size / 1e6, comp_size / 1e6,
        )
        if on_progress:
            on_progress(f"Uploading audio ({comp_size / 1e6:.1f} MB)...")
    else:
        if on_progress:
            on_progress("Compressing audio...")
        upload_path = _compress_audio(audio_path)
        comp_size = Path(upload_path).stat().st_size
        if on_progress:
            on_progress(f"Uploading audio ({comp_size / 1e6:.1f} MB)...")

    config_kwargs = {
        "speaker_labels": True,
        "speech_models": ["universal-3-pro", "universal-2"],
    }

    # Multichannel: each channel transcribed separately (ch0=mic, ch1=loopback)
    # Speaker diarization stays enabled so remote participants on the shared
    # loopback channel can still be separated within that channel.
    if is_multichannel:
        config_kwargs["multichannel"] = True
        logger.info("Using multichannel transcription (stereo)")

    config_kwargs["language_detection"] = True

    if word_boost:
        config_kwargs["word_boost"] = word_boost
        config_kwargs["boost_param"] = "high"
        logger.info("Word boost: %d terms", len(word_boost))

    config = aai.TranscriptionConfig(**config_kwargs)
    transcriber = aai.Transcriber()

    # Step 1: Upload file (heavy — retry on network errors)
    upload_url = None
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1 and on_progress:
                on_progress(f"Retrying upload (attempt {attempt}/{MAX_RETRIES})...")

            upload_url = transcriber.upload_file(upload_path)
            logger.info("Audio uploaded: %s", upload_url)
            break
        except Exception as e:
            last_error = e
            logger.warning("Upload attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                time.sleep(delay)
    else:
        raise last_error  # All upload retries exhausted

    # Step 2: Transcribe from URL (lightweight — no re-upload needed)
    if on_progress:
        on_progress("Transcribing...")

    transcript = transcriber.transcribe(upload_url, config=config)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"Transcription failed: {transcript.error}")

    if on_progress:
        on_progress("Processing complete!")

    utterances = []
    if transcript.utterances:
        for u in transcript.utterances:
            entry = {
                "speaker": u.speaker,
                "text": u.text,
                "start": u.start,
                "end": u.end,
            }
            # Multichannel transcription includes channel info
            channel = getattr(u, "channel", None)
            if channel is not None:
                entry["channel"] = channel
            utterances.append(entry)

    return {
        "utterances": utterances,
        "full_text": transcript.text or "",
        "language": getattr(transcript, "language_code", "unknown"),
        "audio_path": upload_path,  # Actual file used for upload (for Gemini cleanup)
    }
