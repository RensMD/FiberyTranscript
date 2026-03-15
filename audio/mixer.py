"""Mix microphone and system audio streams into a single PCM stream."""

import logging
import threading
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# 100ms of 16-bit mono at 16 kHz → 1600 samples × 2 bytes
MIX_CHUNK_BYTES = 3200

# Max buffering before force-mixing (prevents unbounded latency).
# 5 chunks = 500ms — generous enough to absorb timing jitter between sources.
_OVERFLOW_BYTES = MIX_CHUNK_BYTES * 5


class AudioMixer:
    """Mixes mic and loopback audio into a single stream for transcription."""

    def __init__(
        self,
        on_mixed_chunk: Callable[[bytes], None],
        has_mic: bool = True,
        has_loopback: bool = True,
    ):
        """
        Args:
            on_mixed_chunk: Callback receiving mixed 16-bit PCM mono audio.
            has_mic: Whether a microphone source is active.
            has_loopback: Whether a loopback source is active.
        """
        self._on_mixed_chunk = on_mixed_chunk
        self._mic_buffer = b""
        self._loopback_buffer = b""
        self._lock = threading.Lock()
        self._has_mic = has_mic
        self._has_loopback = has_loopback

    def add_mic_audio(self, pcm_data: bytes) -> None:
        """Add microphone PCM data to the mix buffer."""
        if not pcm_data:
            return
        with self._lock:
            self._mic_buffer += pcm_data
            self._try_mix()

    def add_loopback_audio(self, pcm_data: bytes) -> None:
        """Add loopback/system PCM data to the mix buffer."""
        if not pcm_data:
            return
        with self._lock:
            self._loopback_buffer += pcm_data
            self._try_mix()

    def _try_mix(self) -> None:
        """Mix available audio from both sources and emit.

        When both sources are active, waits for BOTH buffers to have a full
        chunk before mixing.  This prevents one source from constantly
        triggering silence-padded mixes while the other hasn't delivered yet.

        A safety overflow threshold forces a mix if either buffer grows too
        large, preventing unbounded latency when one source stalls.
        """
        while True:
            mic_ready = len(self._mic_buffer) >= MIX_CHUNK_BYTES
            loop_ready = len(self._loopback_buffer) >= MIX_CHUNK_BYTES

            if self._has_mic and self._has_loopback:
                # Both sources active: prefer waiting for both.
                # Force-mix if either buffer overflows to avoid unbounded latency.
                overflow = (
                    len(self._mic_buffer) >= _OVERFLOW_BYTES
                    or len(self._loopback_buffer) >= _OVERFLOW_BYTES
                )
                if not ((mic_ready and loop_ready) or overflow):
                    break
            else:
                # Single source: mix whenever the active source has enough data.
                if not (mic_ready or loop_ready):
                    break

            mic_chunk = self._take_chunk(is_mic=True)
            loop_chunk = self._take_chunk(is_mic=False)

            mic_samples = np.frombuffer(mic_chunk, dtype=np.int16).astype(np.float32)
            loop_samples = np.frombuffer(loop_chunk, dtype=np.int16).astype(np.float32)
            mixed = np.clip(mic_samples + loop_samples, -32768, 32767).astype(np.int16)

            self._on_mixed_chunk(mixed.tobytes())

    def _take_chunk(self, is_mic: bool) -> bytes:
        """Take MIX_CHUNK_BYTES from a buffer, padding with silence if short."""
        if is_mic:
            buf = self._mic_buffer
        else:
            buf = self._loopback_buffer

        if len(buf) >= MIX_CHUNK_BYTES:
            chunk = buf[:MIX_CHUNK_BYTES]
            remainder = buf[MIX_CHUNK_BYTES:]
        else:
            chunk = buf + b"\x00" * (MIX_CHUNK_BYTES - len(buf))
            remainder = b""

        if is_mic:
            self._mic_buffer = remainder
        else:
            self._loopback_buffer = remainder

        return chunk

    def flush(self) -> None:
        """Flush remaining audio in buffers."""
        with self._lock:
            # Mix whatever remains, padding the shorter side with silence
            mic_len = len(self._mic_buffer)
            loop_len = len(self._loopback_buffer)
            flush_len = max(mic_len, loop_len)

            if flush_len == 0:
                return

            mic_data = self._mic_buffer + b"\x00" * (flush_len - mic_len)
            loop_data = self._loopback_buffer + b"\x00" * (flush_len - loop_len)

            mic_samples = np.frombuffer(mic_data, dtype=np.int16).astype(np.float32)
            loop_samples = np.frombuffer(loop_data, dtype=np.int16).astype(np.float32)
            mixed = np.clip(mic_samples + loop_samples, -32768, 32767).astype(np.int16)

            self._on_mixed_chunk(mixed.tobytes())
            self._mic_buffer = b""
            self._loopback_buffer = b""
