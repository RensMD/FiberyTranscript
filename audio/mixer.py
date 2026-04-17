"""Mix microphone and system audio streams into a single PCM stream.

When both mic and loopback are active, outputs stereo (ch0=mic, ch1=loopback).
When only one source is active, outputs mono to halve file size.
"""

import logging
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# 100ms of 16-bit mono at 16 kHz -> 1600 samples x 2 bytes
MIX_CHUNK_BYTES = 3200

# Max buffering before discarding excess (prevents unbounded latency).
# When one source delivers data in bursts (e.g. Bluetooth LE), we cap its
# buffer to avoid accumulating a backlog that forces silence-padded chunks.
_MAX_BUFFER_BYTES = MIX_CHUNK_BYTES * 10  # 1 second
_STALL_TIMEOUT_SECONDS = 0.2


class AudioMixer:
    """Mixes mic and loopback audio into a single stream for recording.

    Stereo output when both sources active (ch0=mic, ch1=loopback).
    Mono output when only one source active.
    """

    def __init__(
        self,
        on_mixed_chunk: Callable[[bytes], None],
        has_mic: bool = True,
        has_loopback: bool = True,
        stall_timeout_seconds: float = _STALL_TIMEOUT_SECONDS,
        output_channels: int | None = None,
    ):
        """
        Args:
            on_mixed_chunk: Callback receiving 16-bit PCM audio (stereo or mono).
            has_mic: Whether a microphone source is active.
            has_loopback: Whether a loopback source is active.
        """
        self._on_mixed_chunk = on_mixed_chunk
        self._mic_buffer = b""
        self._loopback_buffer = b""
        self._lock = threading.Lock()
        # Shared FIFO of mixed chunks ready for downstream emission. Producers
        # append to this queue WHILE HOLDING _lock — that pins enqueue order to
        # _lock acquisition order, which in turn is the authoritative time order
        # for mixed audio. _emit_lock serializes drain so only one thread at a
        # time dispatches, and the drain walks the queue in FIFO order so the
        # emission order always matches the enqueue order regardless of which
        # producer wins the _emit_lock race. This avoids the re-ordering bug
        # of dispatching straight from per-call `pending` lists outside _lock.
        self._emit_queue: deque[bytes] = deque()
        self._emit_lock = threading.Lock()
        self._has_mic = has_mic
        self._has_loopback = has_loopback
        if output_channels is None:
            output_channels = 2 if (has_mic and has_loopback) else 1
        if output_channels not in (1, 2):
            raise ValueError(f"Unsupported output channel count: {output_channels}")
        self._output_channels = output_channels
        self._stall_timeout_seconds = stall_timeout_seconds
        self._stall_warned_mic = False    # True once we've warned about mic stall
        self._stall_warned_loop = False   # True once we've warned about loopback stall
        now = time.monotonic()
        self._last_mic_audio_time = now
        self._last_loopback_audio_time = now
        self._mic_stall_started: float | None = None
        self._loopback_stall_started: float | None = None
        self._mic_seen_audio = not has_mic
        self._loopback_seen_audio = not has_loopback

    @property
    def channels(self) -> int:
        """Number of output channels for emitted PCM."""
        return self._output_channels

    def add_mic_audio(self, pcm_data: bytes) -> None:
        """Add microphone PCM data to the mix buffer."""
        if not pcm_data:
            return
        with self._lock:
            now = time.monotonic()
            self._last_mic_audio_time = now
            self._mic_seen_audio = True
            self._clear_stall(is_mic=True, now=now)
            self._stall_warned_mic = False  # Source is alive again
            self._mic_buffer += pcm_data
            self._cap_buffer()
            pending: list[bytes] = []
            self._try_mix(pending)
            # Enqueue inside _lock so queue order == _lock acquisition order
            self._emit_queue.extend(pending)
        self._drain_emit_queue()

    def add_loopback_audio(self, pcm_data: bytes) -> None:
        """Add loopback/system PCM data to the mix buffer."""
        if not pcm_data:
            return
        with self._lock:
            now = time.monotonic()
            self._last_loopback_audio_time = now
            self._loopback_seen_audio = True
            self._clear_stall(is_mic=False, now=now)
            self._stall_warned_loop = False  # Source is alive again
            self._loopback_buffer += pcm_data
            self._cap_buffer()
            pending: list[bytes] = []
            self._try_mix(pending)
            self._emit_queue.extend(pending)
        self._drain_emit_queue()

    def _drain_emit_queue(self) -> None:
        """Drain the mixed-chunk queue under _emit_lock in FIFO order.

        Any producer thread may call this; only one actually drains at a
        time thanks to _emit_lock, and it drains until empty so we never
        leave chunks stranded waiting for the next producer to show up.
        """
        with self._emit_lock:
            while True:
                try:
                    chunk = self._emit_queue.popleft()
                except IndexError:
                    return
                self._on_mixed_chunk(chunk)

    def _cap_buffer(self) -> None:
        """Discard oldest data if either buffer exceeds the cap.

        When one source delivers data in bursts (e.g. Bluetooth LE loopback),
        its buffer can grow much faster than the other. Without capping, the
        mixer would drain the large buffer by padding the small one with
        silence — producing dead-channel stuttering in stereo mode.

        Instead, we drop the oldest data from the overflowed buffer. This
        loses a bit of audio from the bursty source but keeps both channels
        in sync and stutter-free.
        """
        if len(self._mic_buffer) > _MAX_BUFFER_BYTES:
            excess = len(self._mic_buffer) - _MAX_BUFFER_BYTES
            self._mic_buffer = self._mic_buffer[excess:]
            # Warn once if the loopback side has no data — likely a stalled source
            if self._has_loopback and not self._loopback_buffer and not self._stall_warned_loop:
                logger.warning(
                    "Mixer: loopback source appears stalled (mic buffer capped at %d bytes, "
                    "loopback buffer empty); continuing with silence padding until loopback resumes",
                    _MAX_BUFFER_BYTES,
                )
                self._stall_warned_loop = True
        if len(self._loopback_buffer) > _MAX_BUFFER_BYTES:
            excess = len(self._loopback_buffer) - _MAX_BUFFER_BYTES
            self._loopback_buffer = self._loopback_buffer[excess:]
            # Warn once if the mic side has no data — likely a stalled source
            if self._has_mic and not self._mic_buffer and not self._stall_warned_mic:
                logger.warning(
                    "Mixer: mic source appears stalled (loopback buffer capped at %d bytes, "
                    "mic buffer empty); continuing with silence padding until mic resumes",
                    _MAX_BUFFER_BYTES,
                )
                self._stall_warned_mic = True

    def _mark_stall(self, is_mic: bool, now: float) -> None:
        """Mark a source as stalled and log once until it recovers."""
        if is_mic:
            if self._mic_stall_started is not None:
                return
            self._mic_stall_started = now
            logger.warning("Mixer: microphone source stalled; padding microphone channel with silence")
            return

        if self._loopback_stall_started is not None:
            return
        self._loopback_stall_started = now
        logger.warning("Mixer: loopback source stalled; padding system audio channel with silence")

    def _clear_stall(self, is_mic: bool, now: float) -> None:
        """Clear a stalled-source marker and log recovery once."""
        if is_mic:
            if self._mic_stall_started is None:
                return
            logger.info(
                "Mixer: microphone source recovered after %.3fs",
                now - self._mic_stall_started,
            )
            self._mic_stall_started = None
            return

        if self._loopback_stall_started is None:
            return
        logger.info(
            "Mixer: loopback source recovered after %.3fs",
            now - self._loopback_stall_started,
        )
        self._loopback_stall_started = None

    def _source_stalled(self, is_mic: bool, now: float) -> bool:
        """Return True when a source has been missing beyond the grace window."""
        if is_mic and not self._has_mic:
            return False
        if not is_mic and not self._has_loopback:
            return False

        last_audio_time = self._last_mic_audio_time if is_mic else self._last_loopback_audio_time
        if (now - last_audio_time) < self._stall_timeout_seconds:
            return False

        if is_mic and not self._mic_seen_audio:
            return True
        if not is_mic and not self._loopback_seen_audio:
            return True

        self._mark_stall(is_mic=is_mic, now=now)
        return True

    def _try_mix(self, out: list) -> None:
        """Produce paired chunks and append bytes to `out`.

        In stereo mode, emit paired chunks normally, or pad the missing side
        with silence once a source has stalled past the grace window.
        In mono mode, emits whenever the active source has enough data.

        Callers run this under _lock and dispatch `out` downstream AFTER
        releasing _lock — see _emit_pending().
        """
        while True:
            mic_ready = len(self._mic_buffer) >= MIX_CHUNK_BYTES
            loop_ready = len(self._loopback_buffer) >= MIX_CHUNK_BYTES
            now = time.monotonic()

            if self._has_mic and self._has_loopback:
                # Stereo: use both sources when possible, otherwise let the
                # healthy side continue once the missing side is clearly stalled.
                if not (
                    (mic_ready and loop_ready)
                    or (mic_ready and self._source_stalled(is_mic=False, now=now))
                    or (loop_ready and self._source_stalled(is_mic=True, now=now))
                ):
                    break
            elif self._has_mic:
                if not mic_ready:
                    break
            elif self._has_loopback:
                if not loop_ready:
                    break
            else:
                break

            mic_chunk = self._take_chunk(is_mic=True)
            loop_chunk = self._take_chunk(is_mic=False)

            mic_samples = np.frombuffer(mic_chunk, dtype=np.int16)
            loop_samples = np.frombuffer(loop_chunk, dtype=np.int16)
            out.append(self._emit_chunk(mic_samples, loop_samples))

    def _emit_chunk(self, mic_samples: np.ndarray, loop_samples: np.ndarray) -> bytes:
        """Return one chunk in the configured output format.

        Pure computation — does NOT call downstream callbacks. Caller collects
        the bytes and dispatches them under _emit_lock.
        """
        if self.channels == 2:
            stereo = np.empty(len(mic_samples) * 2, dtype=np.int16)
            stereo[0::2] = mic_samples
            stereo[1::2] = loop_samples
            return stereo.tobytes()

        if self._has_mic and self._has_loopback:
            mixed = np.clip(
                mic_samples.astype(np.int32) + loop_samples.astype(np.int32),
                -32768,
                32767,
            ).astype(np.int16)
            return mixed.tobytes()

        active = mic_samples if self._has_mic else loop_samples
        return active.tobytes()

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

    def deactivate_source(self, source: str) -> None:
        """Mark a source as permanently lost for this mixer instance.

        Drains any remaining audio from the surviving source's buffer and
        prevents _try_mix from waiting on the dead source. The WAV channel
        count is fixed at start() time — DO NOT mutate _output_channels
        here; _emit_chunk's stereo branch already silence-pads the dead
        side via _take_chunk.
        """
        with self._lock:
            if source == "mic":
                self._has_mic = False
            elif source == "loopback":
                self._has_loopback = False
            else:
                logger.warning("deactivate_source: unknown source %r", source)
                return
            logger.info("Mixer: source %r deactivated", source)
            # Drain any partially-buffered surviving-source data.
            pending: list[bytes] = []
            self._try_mix(pending)
            self._emit_queue.extend(pending)
        self._drain_emit_queue()

    def is_source_active(self, source: str) -> bool:
        """Return True if the given source is still contributing audio."""
        with self._lock:
            if source == "mic":
                return self._has_mic
            if source == "loopback":
                return self._has_loopback
            return False

    def flush(self) -> None:
        """Flush remaining audio in buffers.

        Stereo path always interleaves (silence-padding the short side).
        Mono path emits whichever buffer actually has content — importantly,
        this keeps the tail of the recording when the last remaining source
        has been deactivated (both _has_mic and _has_loopback flipped to
        False) right before flush. _emit_chunk's mono single-source branch
        picks via _has_mic, which would emit silence in that post-deactivation
        case; we bypass it here by dispatching the non-empty buffer directly.
        """
        with self._lock:
            mic_len = len(self._mic_buffer)
            loop_len = len(self._loopback_buffer)
            if mic_len == 0 and loop_len == 0:
                self._drain_emit_queue()
                return

            if self.channels == 2:
                flush_len = max(mic_len, loop_len)
                mic_data = self._mic_buffer + b"\x00" * (flush_len - mic_len)
                loop_data = self._loopback_buffer + b"\x00" * (flush_len - loop_len)
                mic_samples = np.frombuffer(mic_data, dtype=np.int16)
                loop_samples = np.frombuffer(loop_data, dtype=np.int16)
                self._emit_queue.append(self._emit_chunk(mic_samples, loop_samples))
            elif self._has_mic and self._has_loopback:
                # Mono with both sources still active — mix them (pad short side)
                flush_len = max(mic_len, loop_len)
                mic_data = self._mic_buffer + b"\x00" * (flush_len - mic_len)
                loop_data = self._loopback_buffer + b"\x00" * (flush_len - loop_len)
                mic_samples = np.frombuffer(mic_data, dtype=np.int16)
                loop_samples = np.frombuffer(loop_data, dtype=np.int16)
                self._emit_queue.append(self._emit_chunk(mic_samples, loop_samples))
            else:
                # Mono single-source (either always was, or other side died).
                # Emit whichever buffer holds real samples.
                if mic_len > 0:
                    self._emit_queue.append(self._mic_buffer)
                else:
                    self._emit_queue.append(self._loopback_buffer)

            self._mic_buffer = b""
            self._loopback_buffer = b""
        self._drain_emit_queue()
