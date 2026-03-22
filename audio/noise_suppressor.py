"""Real-time noise suppression for microphone audio using RNNoise."""

import logging
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)

# Target sample rate for capture pipeline
_SAMPLE_RATE = 16000


class NoiseSuppressor:
    """RNNoise-based noise suppression for 16 kHz mono mic audio.

    pyrnnoise handles 16kHz->48kHz resampling internally. This class
    manages the output buffer to guarantee same-length output for each
    input chunk (pyrnnoise has internal latency that causes per-chunk
    sample count jitter).

    Falls back to pass-through if pyrnnoise is unavailable.
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._available = False
        self._denoiser = None
        self._out_buf: deque = deque()

        if not enabled:
            return

        try:
            from pyrnnoise import RNNoise
            self._denoiser = RNNoise(sample_rate=_SAMPLE_RATE)
            self._available = True
            logger.info("RNNoise noise suppression initialized")
        except ImportError:
            logger.warning("pyrnnoise not installed — noise suppression unavailable")
        except Exception:
            logger.warning("Failed to initialize RNNoise", exc_info=True)

    @property
    def available(self) -> bool:
        """Whether RNNoise loaded successfully."""
        return self._available

    @property
    def enabled(self) -> bool:
        """Whether suppression is active (available AND user toggle on)."""
        return self._enabled and self._available

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    def process(self, samples_int16: np.ndarray) -> np.ndarray:
        """Process a chunk of 16 kHz int16 mono audio.

        Typically 1600 samples (100 ms). Returns cleaned int16 array
        of the same length. Pass-through if not enabled/available.
        """
        if not self.enabled:
            return samples_int16

        try:
            return self._process_internal(samples_int16)
        except Exception:
            logger.debug("RNNoise processing failed, passing through", exc_info=True)
            return samples_int16

    def _process_internal(self, samples_int16: np.ndarray) -> np.ndarray:
        n_needed = len(samples_int16)

        # Convert int16 -> float32 [-1, 1] for pyrnnoise input
        audio_f32 = samples_int16.astype(np.float32) / 32767.0

        # Feed to RNNoise — output is in int16-scale (±32768) as float
        for _vad, denoised in self._denoiser.denoise_chunk(audio_f32, partial=False):
            # denoised shape: (1, frame_len) — flatten and buffer
            self._out_buf.extend(denoised.flatten())

        # Return exactly n_needed samples from buffer
        if len(self._out_buf) >= n_needed:
            out = np.array([self._out_buf.popleft() for _ in range(n_needed)],
                           dtype=np.float32)
        else:
            # Not enough samples yet (startup latency) — pad with zeros
            available = len(self._out_buf)
            out = np.zeros(n_needed, dtype=np.float32)
            for i in range(available):
                out[i] = self._out_buf.popleft()

        # Output is in int16 scale — clip and convert
        return np.clip(out, -32768, 32767).astype(np.int16)

    def reset(self):
        """Reset RNNoise state (call between recordings)."""
        self._out_buf.clear()
        if self._available:
            try:
                from pyrnnoise import RNNoise
                self._denoiser = RNNoise(sample_rate=_SAMPLE_RATE)
            except Exception:
                logger.debug("RNNoise reset failed", exc_info=True)
