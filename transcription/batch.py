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
) -> dict:
    """Transcribe audio file with speaker diarization.

    Args:
        api_key: AssemblyAI API key.
        audio_path: Path to WAV file.
        on_progress: Optional callback for progress updates.
        compressed_path: Pre-compressed file from streaming recording.
            Skips the compression step if provided and valid.

    Returns:
        Dict with 'utterances' (list of speaker-labeled segments),
        'full_text', and 'language'.
    """
    import assemblyai as aai

    aai.settings.api_key = api_key

    # Normalize audio levels before compression/upload
    try:
        from audio.normalizer import normalize_audio
        if on_progress:
            on_progress("Normalizing audio levels...")
        normalize_audio(Path(audio_path))
    except Exception:
        logger.debug("Audio normalization skipped", exc_info=True)

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

    # Clean up temporary compressed file (not the pre-compressed one from recorder)
    if upload_path != audio_path and upload_path != compressed_path:
        try:
            Path(upload_path).unlink()
        except OSError:
            pass

    if on_progress:
        on_progress("Processing complete!")

    utterances = []
    if transcript.utterances:
        for u in transcript.utterances:
            utterances.append({
                "speaker": u.speaker,
                "text": u.text,
                "start": u.start,
                "end": u.end,
            })

    return {
        "utterances": utterances,
        "full_text": transcript.text or "",
        "language": getattr(transcript, "language_code", "unknown"),
    }
