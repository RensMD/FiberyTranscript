"""Pre-upload audio normalization for transcription quality."""

import logging
import tempfile
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_CHUNK_FRAMES = 65536


def normalize_audio(wav_path: Path, target_peak_db: float = -1.0) -> Path:
    """Peak-normalize a WAV file in-place for maximum dynamic range.

    Two-pass: first finds peak, then scales all samples so the peak
    hits target_peak_db (default -1.0 dBFS for headroom).

    Returns the same path. Falls back to no-op on error.
    """
    try:
        import soundfile as sf
    except ImportError:
        logger.debug("soundfile not available, skipping normalization")
        return wav_path

    try:
        # Pass 1: find peak amplitude
        peak = 0.0
        with sf.SoundFile(str(wav_path), 'r') as f:
            samplerate = f.samplerate
            channels = f.channels
            subtype = f.subtype
            while True:
                chunk = f.read(_CHUNK_FRAMES, dtype='float32')
                if len(chunk) == 0:
                    break
                chunk_peak = float(np.max(np.abs(chunk)))
                if chunk_peak > peak:
                    peak = chunk_peak

        if peak < 1e-6:
            logger.debug("Audio is silent, skipping normalization")
            return wav_path

        # Calculate gain
        target_peak = 10 ** (target_peak_db / 20.0)
        gain = target_peak / peak

        # Skip if already within 1 dB of target
        if abs(20 * np.log10(peak / target_peak)) < 1.0:
            logger.debug("Audio peak already near target, skipping normalization")
            return wav_path

        logger.info("Normalizing audio: peak=%.4f, gain=%.2fx (%.1f dB)",
                     peak, gain, 20 * np.log10(gain))

        # Pass 2: apply gain, write to temp file
        tmp_path = wav_path.with_suffix('.norm.wav')
        try:
            with sf.SoundFile(str(wav_path), 'r') as src:
                with sf.SoundFile(str(tmp_path), 'w',
                                  samplerate=samplerate,
                                  channels=channels,
                                  subtype=subtype) as dst:
                    while True:
                        chunk = src.read(_CHUNK_FRAMES, dtype='float32')
                        if len(chunk) == 0:
                            break
                        chunk = np.clip(chunk * gain, -1.0, 1.0)
                        dst.write(chunk)

            # Replace original with normalized version
            import os
            os.replace(str(tmp_path), str(wav_path))
            logger.info("Audio normalized successfully")
        except Exception:
            # Clean up temp file on failure
            tmp_path.unlink(missing_ok=True)
            raise

        return wav_path

    except Exception:
        logger.warning("Audio normalization failed, using original", exc_info=True)
        return wav_path
