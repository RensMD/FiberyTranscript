"""Audio health monitoring during recording.

Tracks per-channel state (alive, clipping, speech) and reports
a snapshot every ~2 seconds for the UI to display indicators.
"""

import logging
import time
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# Thresholds — class constants, easy to tune
DEAD_CHANNEL_THRESHOLD = 0.001   # RMS below this = "silent"
DEAD_CHANNEL_DURATION = 5.0      # seconds before marking dead
CLIPPING_THRESHOLD = 0.95        # RMS above this = clipping
CLIPPING_DURATION = 0.5          # seconds before warning
SPEECH_THRESHOLD = 0.02          # RMS above this = likely speech


@dataclass
class AudioHealth:
    mic_alive: bool = True
    sys_alive: bool = True
    mic_clipping: bool = False
    sys_clipping: bool = False
    speech_detected: bool = False
    silence_duration: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class AudioHealthMonitor:
    """Tracks audio health from RMS level updates (~10/sec)."""

    TICK_INTERVAL = 0.1  # expected interval between updates

    def __init__(self):
        self._mic_silent_since: float = 0.0
        self._sys_silent_since: float = 0.0
        self._mic_clip_since: float = 0.0
        self._sys_clip_since: float = 0.0
        self._speech_last_seen: float = 0.0
        self._both_silent_since: float = 0.0
        self._last_report: float = 0.0
        self._started: float = time.monotonic()
        # Toast dedup — only fire once per event
        self._mic_dead_toasted: bool = False
        self._both_dead_toasted: bool = False
        self._clipping_toasted: bool = False

    def reset(self):
        """Reset all tracking state (call on recording start)."""
        now = time.monotonic()
        self._mic_silent_since = 0.0
        self._sys_silent_since = 0.0
        self._mic_clip_since = 0.0
        self._sys_clip_since = 0.0
        self._speech_last_seen = 0.0
        self._both_silent_since = 0.0
        self._last_report = 0.0
        self._started = now
        self._mic_dead_toasted = False
        self._both_dead_toasted = False
        self._clipping_toasted = False

    def update(self, mic_rms: float, sys_rms: float) -> "AudioHealth | None":
        """Process one RMS tick. Returns AudioHealth snapshot every ~2s, else None."""
        now = time.monotonic()

        # --- Mic channel ---
        if mic_rms < 0:
            # No mic active
            pass
        elif mic_rms < DEAD_CHANNEL_THRESHOLD:
            if self._mic_silent_since == 0.0:
                self._mic_silent_since = now
        else:
            self._mic_silent_since = 0.0
            self._mic_dead_toasted = False

        if mic_rms > CLIPPING_THRESHOLD:
            if self._mic_clip_since == 0.0:
                self._mic_clip_since = now
        else:
            self._mic_clip_since = 0.0
            self._clipping_toasted = False

        # --- Sys channel ---
        if sys_rms < 0:
            pass
        elif sys_rms < DEAD_CHANNEL_THRESHOLD:
            if self._sys_silent_since == 0.0:
                self._sys_silent_since = now
        else:
            self._sys_silent_since = 0.0

        if sys_rms > CLIPPING_THRESHOLD:
            if self._sys_clip_since == 0.0:
                self._sys_clip_since = now
        else:
            self._sys_clip_since = 0.0

        # --- Speech detection ---
        if max(mic_rms, sys_rms) > SPEECH_THRESHOLD:
            self._speech_last_seen = now

        # --- Both silent tracking ---
        mic_dead = mic_rms >= 0 and self._mic_silent_since > 0 and (now - self._mic_silent_since) > DEAD_CHANNEL_DURATION
        sys_dead = sys_rms >= 0 and self._sys_silent_since > 0 and (now - self._sys_silent_since) > DEAD_CHANNEL_DURATION
        if mic_dead and sys_dead:
            if self._both_silent_since == 0.0:
                self._both_silent_since = now
        else:
            self._both_silent_since = 0.0
            self._both_dead_toasted = False

        # --- Throttled report (every 2s) ---
        if now - self._last_report < 2.0:
            return None
        self._last_report = now

        mic_alive = mic_rms < 0 or not mic_dead  # mic_rms < 0 means no mic selected
        sys_alive = sys_rms < 0 or not sys_dead
        mic_clipping = self._mic_clip_since > 0 and (now - self._mic_clip_since) > CLIPPING_DURATION
        sys_clipping = self._sys_clip_since > 0 and (now - self._sys_clip_since) > CLIPPING_DURATION
        speech_detected = self._speech_last_seen > 0 and (now - self._speech_last_seen) < 300  # 5 min
        silence_duration = (now - self._speech_last_seen) if self._speech_last_seen > 0 else (now - self._started)

        return AudioHealth(
            mic_alive=mic_alive,
            sys_alive=sys_alive,
            mic_clipping=mic_clipping,
            sys_clipping=sys_clipping,
            speech_detected=speech_detected,
            silence_duration=silence_duration,
        )

    def check_warnings(self, health: "AudioHealth") -> list[str]:
        """Return list of warning messages to toast (deduped, fires once per event)."""
        warnings = []
        if not health.mic_alive and not self._mic_dead_toasted:
            self._mic_dead_toasted = True
            if health.sys_alive:
                warnings.append("Microphone disconnected. Recording continues with system audio only.")
            logger.debug("Mic channel dead")

        if not health.mic_alive and not health.sys_alive and not self._both_dead_toasted:
            self._both_dead_toasted = True
            warnings.append("BOTH_DEAD:Both audio sources are silent — stopping recording.")
            logger.warning("Both channels dead — requesting auto-stop")

        if (health.mic_clipping or health.sys_clipping) and not self._clipping_toasted:
            self._clipping_toasted = True
            warnings.append("Audio is clipping. Try reducing input volume.")
            logger.debug("Audio clipping detected")

        return warnings
