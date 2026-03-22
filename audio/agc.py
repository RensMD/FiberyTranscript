"""Automatic Gain Control for microphone audio."""

import logging

import numpy as np

logger = logging.getLogger(__name__)


class AutomaticGainControl:
    """Simple digital AGC for mic audio.

    Smoothly adjusts gain to bring average speech level to a target RMS.
    Only boosts (never attenuates). Does NOT amplify silence thanks to
    a noise gate threshold.
    """

    def __init__(
        self,
        target_rms: float = 0.1,
        max_gain: float = 10.0,
        attack_time: float = 0.01,
        release_time: float = 0.3,
        noise_gate: float = 0.002,
        sample_rate: int = 16000,
        enabled: bool = True,
    ):
        self._enabled = enabled
        self._target_rms = target_rms
        self._max_gain = max_gain
        self._noise_gate = noise_gate
        self._current_gain = 1.0

        # Smoothing coefficients (exponential moving average)
        chunk_duration = 0.1  # 100 ms chunks
        self._attack_alpha = min(1.0, chunk_duration / max(attack_time, 1e-6))
        self._release_alpha = min(1.0, chunk_duration / max(release_time, 1e-6))

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    @property
    def current_gain(self) -> float:
        return self._current_gain

    def process(self, samples_int16: np.ndarray) -> np.ndarray:
        """Apply AGC to int16 samples. Returns int16."""
        if not self._enabled:
            return samples_int16

        try:
            return self._process_internal(samples_int16)
        except Exception:
            logger.debug("AGC processing failed, passing through", exc_info=True)
            return samples_int16

    def _process_internal(self, samples_int16: np.ndarray) -> np.ndarray:
        audio_f32 = samples_int16.astype(np.float32) / 32767.0

        # Calculate RMS of this chunk
        rms = float(np.sqrt(np.mean(audio_f32 ** 2)))

        if rms < self._noise_gate:
            # Below noise gate — apply current gain without adjusting it
            desired_gain = self._current_gain
        else:
            # Compute desired gain, clamp to [1.0, max_gain]
            desired_gain = min(self._max_gain, max(1.0, self._target_rms / rms))

            # Smooth gain transition
            if desired_gain > self._current_gain:
                alpha = self._attack_alpha
            else:
                alpha = self._release_alpha
            self._current_gain += alpha * (desired_gain - self._current_gain)

        # Apply gain
        amplified = audio_f32 * self._current_gain

        # Clip and convert back to int16
        return np.clip(amplified * 32767.0, -32768, 32767).astype(np.int16)

    def reset(self):
        """Reset gain state (call between recordings)."""
        self._current_gain = 1.0
