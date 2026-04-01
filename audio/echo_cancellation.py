"""Echo cancellation using loopback audio as reference.

Removes acoustic echo from the microphone channel by subtracting the
estimated echo component using frequency-domain spectral subtraction.
The loopback channel provides a clean reference of what the speakers
were playing, enabling precise echo estimation.

Uses only numpy and scipy (already project dependencies).
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Processing parameters
_FRAME_SIZE = 1024       # STFT frame size in samples (64ms at 16kHz)
_HOP_SIZE = 512          # 50% overlap
_SMOOTHING = 0.85        # Transfer function smoothing (EMA alpha for history)
_SUBTRACTION_FACTOR = 1.0  # How aggressively to subtract echo (1.0 = full)
_SPECTRAL_FLOOR = 0.01  # -40dB floor prevents musical noise artifacts
_SILENCE_THRESHOLD = 1e-6  # RMS below this = silence (skip processing)

# Streaming chunk size — process this many frames at a time for memory efficiency
_STREAM_CHUNK_FRAMES = 32768  # ~2 seconds at 16kHz


def cancel_echo(
    mic: np.ndarray,
    loopback: np.ndarray,
    sample_rate: int = 16000,
    delay: Optional[int] = None,
) -> np.ndarray:
    """Remove echo from mic signal using loopback as reference.

    Args:
        mic: Microphone audio (float32, -1.0 to 1.0).
        loopback: Loopback/system audio (float32, -1.0 to 1.0).
        sample_rate: Sample rate in Hz.
        delay: Pre-computed echo delay in samples. If None, estimated via
               cross-correlation (expensive). Pass a pre-computed value when
               processing multiple chunks to avoid per-chunk re-estimation.

    Returns:
        Cleaned microphone audio (float32, same length as input).
    """
    if len(mic) != len(loopback):
        raise ValueError("mic and loopback must have the same length")

    if len(mic) < _FRAME_SIZE:
        return mic.copy()

    # Skip if loopback is silent (no echo to cancel)
    if np.sqrt(np.mean(loopback ** 2)) < _SILENCE_THRESHOLD:
        logger.debug("Loopback is silent, skipping echo cancellation")
        return mic.copy()

    # Find delay between loopback and its echo in mic via cross-correlation
    if delay is None:
        delay = _estimate_delay(mic, loopback, sample_rate)
    if delay > 0:
        # Align loopback to match echo timing in mic
        aligned_loopback = np.zeros_like(loopback)
        aligned_loopback[delay:] = loopback[:len(loopback) - delay]
    else:
        aligned_loopback = loopback

    # STFT of both signals
    from scipy.signal import stft, istft

    _, _, mic_stft = stft(
        mic, fs=sample_rate, nperseg=_FRAME_SIZE, noverlap=_HOP_SIZE,
        window="hann", boundary=None, padded=False,
    )
    _, _, loop_stft = stft(
        aligned_loopback, fs=sample_rate, nperseg=_FRAME_SIZE, noverlap=_HOP_SIZE,
        window="hann", boundary=None, padded=False,
    )

    mic_mag = np.abs(mic_stft)
    mic_phase = np.angle(mic_stft)
    loop_mag = np.abs(loop_stft)

    # Estimate and smooth the transfer function H across frames
    n_freq, n_frames = mic_mag.shape
    H_smooth = np.zeros(n_freq, dtype=np.float64)

    cleaned_mag = np.empty_like(mic_mag)
    for t in range(n_frames):
        # Only update H where loopback has energy
        loop_energy = loop_mag[:, t]
        has_signal = loop_energy > _SILENCE_THRESHOLD

        # Instantaneous transfer function estimate
        H_inst = np.zeros(n_freq, dtype=np.float64)
        H_inst[has_signal] = mic_mag[has_signal, t] / (loop_energy[has_signal] + 1e-10)

        # Exponential moving average smoothing
        H_smooth[has_signal] = (
            _SMOOTHING * H_smooth[has_signal]
            + (1 - _SMOOTHING) * H_inst[has_signal]
        )

        # Estimate echo magnitude
        echo_est = H_smooth * loop_energy

        # Spectral subtraction with floor
        cleaned = mic_mag[:, t] - _SUBTRACTION_FACTOR * echo_est
        floor = _SPECTRAL_FLOOR * mic_mag[:, t]
        cleaned_mag[:, t] = np.maximum(cleaned, floor)

    # Reconstruct with original mic phase
    cleaned_stft = cleaned_mag * np.exp(1j * mic_phase)
    _, cleaned = istft(
        cleaned_stft, fs=sample_rate, nperseg=_FRAME_SIZE, noverlap=_HOP_SIZE,
        window="hann", boundary=False,
    )

    # Match output length to input
    if len(cleaned) > len(mic):
        cleaned = cleaned[:len(mic)]
    elif len(cleaned) < len(mic):
        cleaned = np.pad(cleaned, (0, len(mic) - len(cleaned)))

    return cleaned.astype(np.float32)


def _estimate_delay(
    mic: np.ndarray,
    loopback: np.ndarray,
    sample_rate: int,
    max_delay_ms: float = 200.0,
) -> int:
    """Estimate the delay between loopback signal and its echo in mic.

    Uses cross-correlation on a segment of the audio to find the lag
    where the loopback best matches the mic signal.

    Args:
        mic: Microphone audio.
        loopback: Loopback audio.
        sample_rate: Sample rate in Hz.
        max_delay_ms: Maximum expected delay in milliseconds.

    Returns:
        Delay in samples (0 if no significant correlation found).
    """
    max_delay_samples = int(sample_rate * max_delay_ms / 1000)
    seg_len = sample_rate * 5  # 5-second analysis window

    # Scan forward to find a segment where loopback has energy.
    # The meeting may start with silence or mic-only speech.
    mic_seg = None
    loop_seg = None
    for offset in range(0, min(len(mic), sample_rate * 30), seg_len):
        end = min(offset + seg_len, len(mic))
        candidate_loop = loopback[offset:end]
        if np.sqrt(np.mean(candidate_loop ** 2)) > 0.01:
            mic_seg = mic[offset:end]
            loop_seg = candidate_loop
            break

    if mic_seg is None:
        logger.debug("No active loopback segment found for delay estimation")
        return 0

    # Cross-correlate
    from scipy.signal import correlate

    correlation = correlate(mic_seg, loop_seg, mode="full")
    mid = len(loop_seg) - 1  # zero-lag index

    # Only look at positive delays (mic lags behind loopback due to acoustic path)
    search_region = correlation[mid:mid + max_delay_samples]

    if len(search_region) == 0:
        return 0

    peak_idx = int(np.argmax(np.abs(search_region)))

    # Check if the correlation peak is significant
    peak_val = abs(search_region[peak_idx])
    mean_val = np.mean(np.abs(search_region))
    if mean_val > 0 and peak_val / mean_val > 3.0:
        logger.debug("Echo delay estimated: %d samples (%.1f ms)", peak_idx, peak_idx / sample_rate * 1000)
        return peak_idx

    logger.debug("No significant echo delay found")
    return 0


def process_stereo_file(
    input_path: Path,
    output_path: Optional[Path] = None,
    sample_rate: int = 16000,
) -> Path:
    """Process a stereo WAV file: cancel echo from ch0 (mic) using ch1 (loopback).

    Reads and processes in chunks for memory efficiency. The loopback channel
    (ch1) is preserved unchanged in the output.

    Args:
        input_path: Path to stereo WAV (ch0=mic, ch1=loopback).
        output_path: Where to write result. Defaults to input_stem + '_echocancelled.wav'.

    Returns:
        Path to the output file. Returns input_path unchanged on error.
    """
    try:
        import soundfile as sf
    except ImportError:
        logger.warning("soundfile not available, skipping echo cancellation")
        return input_path

    if output_path is None:
        output_path = input_path.parent / f"{input_path.stem}_echocancelled.wav"

    try:
        info = sf.info(str(input_path))
        if info.channels < 2:
            logger.debug("Mono file, nothing to echo-cancel")
            return input_path

        # First pass: read initial audio for delay estimation.
        # Read up to 30s so _estimate_delay can find a segment with loopback energy.
        with sf.SoundFile(str(input_path), "r") as src:
            initial = src.read(sample_rate * 30)
            if len(initial) == 0:
                return input_path
            mic_init = initial[:, 0].astype(np.float32)
            loop_init = initial[:, 1].astype(np.float32)

        # Check if loopback has meaningful audio anywhere in the initial segment
        loop_rms = float(np.sqrt(np.mean(loop_init ** 2)))
        if loop_rms < _SILENCE_THRESHOLD:
            logger.info("Loopback channel is silent, skipping echo cancellation")
            return input_path

        delay = _estimate_delay(mic_init, loop_init, sample_rate)

        # Second pass: process full file in chunks with overlap
        overlap_samples = _FRAME_SIZE  # overlap between chunks for continuity
        with sf.SoundFile(str(input_path), "r") as src:
            with sf.SoundFile(
                str(output_path), "w",
                samplerate=src.samplerate,
                channels=2,
                subtype=src.subtype,
            ) as dst:
                carry_mic = np.array([], dtype=np.float32)
                carry_loop = np.array([], dtype=np.float32)

                while True:
                    chunk = src.read(_STREAM_CHUNK_FRAMES)
                    if len(chunk) == 0:
                        break

                    mic_chunk = np.concatenate([carry_mic, chunk[:, 0].astype(np.float32)])
                    loop_chunk = np.concatenate([carry_loop, chunk[:, 1].astype(np.float32)])

                    if len(mic_chunk) < _FRAME_SIZE * 2:
                        # Too short to process, write as-is
                        out = np.column_stack([mic_chunk, loop_chunk])
                        dst.write(out)
                        carry_mic = np.array([], dtype=np.float32)
                        carry_loop = np.array([], dtype=np.float32)
                        continue

                    # Process, keeping overlap for next chunk
                    process_len = len(mic_chunk) - overlap_samples
                    cleaned = cancel_echo(mic_chunk, loop_chunk, sample_rate, delay=delay)

                    # Write processed portion (excluding overlap carry)
                    out = np.column_stack([
                        cleaned[:process_len],
                        loop_chunk[:process_len],
                    ])
                    dst.write(out)

                    # Carry overlap into next chunk
                    carry_mic = mic_chunk[process_len:]
                    carry_loop = loop_chunk[process_len:]

                # Flush remaining carry
                if len(carry_mic) > 0:
                    # Process or pass through the remaining samples
                    if len(carry_mic) >= _FRAME_SIZE:
                        cleaned = cancel_echo(carry_mic, carry_loop, sample_rate, delay=delay)
                    else:
                        cleaned = carry_mic
                    out = np.column_stack([cleaned, carry_loop])
                    dst.write(out)

        logger.info("Echo cancellation complete: %s", output_path.name)
        return output_path

    except Exception:
        logger.warning("Echo cancellation failed, using original", exc_info=True)
        # Clean up partial output
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        return input_path
