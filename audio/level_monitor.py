"""Audio level calculation for visualization."""

import numpy as np


def calculate_rms(samples: np.ndarray) -> float:
    """Calculate RMS level from audio samples.

    Args:
        samples: float32 array normalized to [-1.0, 1.0] range.

    Returns:
        RMS value in 0.0-1.0 range.
    """
    if len(samples) == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    return min(1.0, rms)
