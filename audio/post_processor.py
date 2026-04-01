"""Post-recording audio processing pipeline.

Processes a raw stereo WAV (ch0=mic, ch1=loopback) through a sequence
of enhancement steps before transcription upload. Each step is optional
and gracefully falls back on failure. The raw file is always preserved.

Pipeline order:
1. Echo cancellation (mic channel, using loopback as reference)
2. Noise suppression + AGC (mic channel, combined pass)
3. Per-channel peak normalization
"""

import logging
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Chunk size for noise suppression + AGC pass (must match NoiseSuppressor expectations)
_NS_CHUNK_SAMPLES = 1600  # 100ms at 16kHz

# Chunk size for normalization passes
_NORM_CHUNK_FRAMES = 65536


class PostProcessor:
    """Post-recording audio processing pipeline.

    Takes a raw stereo WAV (ch0=mic, ch1=loopback) and produces a
    processed version ready for transcription. The raw file is preserved.
    """

    def __init__(
        self,
        echo_cancel: bool = True,
        noise_suppress: bool = True,
        agc: bool = True,
        normalize: bool = True,
    ):
        self._echo_cancel = echo_cancel
        self._noise_suppress = noise_suppress
        self._agc = agc
        self._normalize = normalize

    def process(
        self,
        raw_wav_path: Path,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """Process the raw recording.

        Returns path to processed WAV (*_processed.wav alongside the raw file).
        On complete failure, returns the original raw path.
        """
        if not any((self._echo_cancel, self._noise_suppress, self._agc, self._normalize)):
            logger.info("Post-processing enabled, but all stages are off; using original audio")
            return raw_wav_path

        try:
            import soundfile as sf
        except ImportError:
            logger.warning("soundfile not available, skipping post-processing")
            return raw_wav_path

        try:
            info = sf.info(str(raw_wav_path))
        except Exception:
            logger.warning("Cannot read audio info, skipping post-processing", exc_info=True)
            return raw_wav_path

        is_stereo = info.channels >= 2
        current_path = raw_wav_path
        temp_files: list[Path] = []

        try:
            # Step 1: Echo cancellation (stereo only, when loopback has audio)
            if self._echo_cancel and is_stereo:
                if on_progress:
                    on_progress("Removing echo...")
                current_path = self._run_echo_cancellation(current_path, temp_files)

            # Step 2: Noise suppression + AGC (mic channel)
            if self._noise_suppress or self._agc:
                if on_progress:
                    on_progress("Enhancing audio...")
                current_path = self._run_denoise_agc(
                    current_path, is_stereo, temp_files,
                )

            # Step 3: Peak normalization (all channels)
            if self._normalize:
                if on_progress:
                    on_progress("Normalizing levels...")
                current_path = self._run_normalization(current_path, temp_files)

            if current_path == raw_wav_path:
                logger.info("Post-processing made no changes; using original audio")
                return raw_wav_path

            # Write final output as *_processed.wav
            output_path = raw_wav_path.parent / f"{raw_wav_path.stem}_processed.wav"
            import shutil
            shutil.move(str(current_path), str(output_path))
            # Remove from temp list since we moved it
            if current_path in temp_files:
                temp_files.remove(current_path)

            logger.info("Post-processing complete: %s", output_path.name)
            return output_path

        except Exception:
            logger.warning("Post-processing pipeline failed, using raw audio", exc_info=True)
            return raw_wav_path

        finally:
            # Clean up any temp files
            for tmp in temp_files:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def _run_echo_cancellation(
        self,
        input_path: Path,
        temp_files: list[Path],
    ) -> Path:
        """Run echo cancellation. Returns new path or input_path on failure."""
        try:
            from audio.echo_cancellation import process_stereo_file

            output_path = input_path.parent / f"{input_path.stem}_ec.wav"
            temp_files.append(output_path)
            result = process_stereo_file(input_path, output_path)
            if result == input_path:
                # Echo cancellation was skipped (e.g. silent loopback)
                temp_files.remove(output_path)
            return result
        except Exception:
            logger.warning("Echo cancellation step failed", exc_info=True)
            return input_path

    def _run_denoise_agc(
        self,
        input_path: Path,
        is_stereo: bool,
        temp_files: list[Path],
    ) -> Path:
        """Run noise suppression and/or AGC on mic channel.

        Processes in 1600-sample (100ms) chunks matching the existing
        NoiseSuppressor and AGC interfaces.
        """
        try:
            import soundfile as sf
        except ImportError:
            return input_path

        # Initialize processors
        suppressor = None
        agc = None

        if self._noise_suppress:
            try:
                from audio.noise_suppressor import NoiseSuppressor
                suppressor = NoiseSuppressor(enabled=True)
                if not suppressor.available:
                    logger.debug("RNNoise not available, skipping noise suppression")
                    suppressor = None
            except Exception:
                logger.debug("Could not initialize noise suppressor", exc_info=True)

        if self._agc:
            try:
                from audio.agc import AutomaticGainControl
                agc = AutomaticGainControl(enabled=True)
            except Exception:
                logger.debug("Could not initialize AGC", exc_info=True)

        if suppressor is None and agc is None:
            return input_path

        output_path = input_path.parent / f"{input_path.stem}_da.wav"
        temp_files.append(output_path)

        try:
            with sf.SoundFile(str(input_path), "r") as src:
                with sf.SoundFile(
                    str(output_path), "w",
                    samplerate=src.samplerate,
                    channels=src.channels,
                    subtype=src.subtype,
                ) as dst:
                    while True:
                        chunk = src.read(_NS_CHUNK_SAMPLES, dtype="float32")
                        if len(chunk) == 0:
                            break

                        if is_stereo and chunk.ndim == 2:
                            mic = chunk[:, 0]
                            loopback = chunk[:, 1]
                        else:
                            mic = chunk if chunk.ndim == 1 else chunk[:, 0]
                            loopback = None

                        # Convert mic to int16 for processors
                        mic_int16 = np.clip(mic * 32767.0, -32768, 32767).astype(np.int16)

                        if suppressor is not None:
                            mic_int16 = suppressor.process(mic_int16)

                        if agc is not None:
                            mic_int16 = agc.process(mic_int16)

                        # Convert back to float32
                        mic_out = mic_int16.astype(np.float32) / 32767.0

                        if is_stereo and loopback is not None:
                            out = np.column_stack([mic_out, loopback])
                        else:
                            out = mic_out

                        dst.write(out)

            logger.info("Denoise/AGC complete")
            return output_path

        except Exception:
            logger.warning("Denoise/AGC step failed", exc_info=True)
            if output_path.exists():
                try:
                    output_path.unlink()
                except OSError:
                    pass
            temp_files.remove(output_path)
            return input_path

    def _run_normalization(
        self,
        input_path: Path,
        temp_files: list[Path],
    ) -> Path:
        """Peak-normalize each channel independently to -1 dBFS."""
        try:
            import soundfile as sf
        except ImportError:
            return input_path

        output_path = input_path.parent / f"{input_path.stem}_norm.wav"
        temp_files.append(output_path)

        target_peak_db = -1.0
        target_peak = 10 ** (target_peak_db / 20.0)

        try:
            # Pass 1: find per-channel peaks
            with sf.SoundFile(str(input_path), "r") as src:
                n_channels = src.channels
                peaks = np.zeros(n_channels, dtype=np.float64)

                while True:
                    chunk = src.read(_NORM_CHUNK_FRAMES, dtype="float32")
                    if len(chunk) == 0:
                        break
                    if chunk.ndim == 1:
                        chunk = chunk.reshape(-1, 1)
                    for ch in range(n_channels):
                        ch_peak = float(np.max(np.abs(chunk[:, ch])))
                        if ch_peak > peaks[ch]:
                            peaks[ch] = ch_peak

            # Calculate per-channel gains
            gains = np.ones(n_channels, dtype=np.float64)
            for ch in range(n_channels):
                if peaks[ch] < 1e-6:
                    continue  # Silent channel, leave at unity
                gain = target_peak / peaks[ch]
                # Skip if already within 1 dB
                if abs(20 * np.log10(peaks[ch] / target_peak)) < 1.0:
                    continue
                gains[ch] = gain

            if np.allclose(gains, 1.0):
                logger.debug("All channels already near target, skipping normalization")
                temp_files.remove(output_path)
                return input_path

            logger.info(
                "Normalizing: peaks=%s, gains=%s",
                [f"{p:.4f}" for p in peaks],
                [f"{g:.2f}x" for g in gains],
            )

            # Pass 2: apply per-channel gains
            with sf.SoundFile(str(input_path), "r") as src:
                with sf.SoundFile(
                    str(output_path), "w",
                    samplerate=src.samplerate,
                    channels=src.channels,
                    subtype=src.subtype,
                ) as dst:
                    while True:
                        chunk = src.read(_NORM_CHUNK_FRAMES, dtype="float32")
                        if len(chunk) == 0:
                            break
                        if chunk.ndim == 1:
                            chunk = chunk.reshape(-1, 1)
                        for ch in range(n_channels):
                            chunk[:, ch] *= gains[ch]
                        chunk = np.clip(chunk, -1.0, 1.0)
                        if n_channels == 1:
                            chunk = chunk.flatten()
                        dst.write(chunk)

            return output_path

        except Exception:
            logger.warning("Normalization step failed", exc_info=True)
            if output_path.exists():
                try:
                    output_path.unlink()
                except OSError:
                    pass
            temp_files.remove(output_path)
            return input_path
