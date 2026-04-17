"""WAV file recorder for saving audio during capture sessions.

Writes raw stereo audio to WAV (sacred debug artifact). Optionally writes
a processed + compressed OGG Vorbis copy in parallel: mic channel gets
noise suppression + AGC applied before OGG encoding, while the WAV stays raw.

OGG processing runs on a background thread to avoid blocking the audio
capture callbacks (RNNoise is too slow for the critical path).
"""

import logging
import os
import queue
import threading
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from config.constants import SAMPLE_RATE
from utils.filename_utils import build_recording_stem, truncate_stem_for_directory

logger = logging.getLogger(__name__)

# Check for soundfile (OGG Vorbis support) at import time
try:
    import soundfile as sf
    _HAS_SOUNDFILE = True
except ImportError:
    _HAS_SOUNDFILE = False

_SENTINEL = None  # Poison pill for OGG writer thread

# Header refresh cadence: every 30s an off-path worker rewrites the WAV header
# so a crash/power-fail leaves a playable file up to the last tick.
_WAV_HEADER_REFRESH_SECONDS = 30.0
# 32-bit WAV header size ceiling (riff_size field). Above this we stop
# refreshing — the wave module's own close() will produce a valid header
# up to its own limit.
_WAV_HEADER_SIZE_LIMIT = 0xFFFFFFFF - 36


class WavRecorder:
    """Records PCM audio chunks to a WAV file.

    WAV: raw stereo audio (untouched, for debugging and reprocessing).
    OGG: processed audio (mic channel has denoise + AGC applied) for
    faster upload. The OGG serves as a fallback if post-recording
    processing fails.

    OGG processing (RNNoise + AGC) runs on a dedicated background thread
    so it never blocks the audio capture callbacks.
    """

    def __init__(
        self,
        output_dir: Path,
        sample_rate: int = SAMPLE_RATE,
        noise_suppressor=None,
        agc=None,
        channels: int = 2,
        meeting_name: str = "",
    ):
        self._output_dir = output_dir
        self._sample_rate = sample_rate
        self._noise_suppressor = noise_suppressor
        self._agc = agc
        self._channels = channels
        self._meeting_name = meeting_name
        self._wav_file: Optional[wave.Wave_write] = None
        self._ogg_file = None  # sf.SoundFile or None
        self._file_path: Optional[Path] = None
        self._ogg_path: Optional[Path] = None
        self._lock = threading.Lock()  # Protects WAV file only
        self._ogg_queue: Optional[queue.Queue] = None
        self._ogg_thread: Optional[threading.Thread] = None
        self._ogg_dropped_chunks: int = 0
        self._ogg_is_complete: bool = True
        # Crash-safe WAV: a background worker periodically rewrites the RIFF
        # header so a hard kill still leaves a playable file. Header size is
        # derived from the actual on-disk file length (not an in-memory
        # counter) so the header can never claim audio that hasn't been
        # persisted — see _header_refresh_loop.
        self._header_refresh_thread: Optional[threading.Thread] = None
        self._header_refresh_stop = threading.Event()

    def _build_unique_path(self, base_stem: str, suffix: str) -> Path:
        """Return a non-conflicting path in the output directory."""
        safe_stem = truncate_stem_for_directory(base_stem, self._output_dir, suffix)
        candidate = self._output_dir / f"{safe_stem}{suffix}"
        if not candidate.exists():
            return candidate
        counter = 2
        while True:
            candidate = self._output_dir / f"{safe_stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def start(self) -> Path:
        """Start recording to a new WAV file. Returns the file path."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        base_stem = build_recording_stem(self._meeting_name)
        self._file_path = self._build_unique_path(base_stem, ".wav")

        self._wav_file = wave.open(str(self._file_path), "wb")
        self._wav_file.setnchannels(self._channels)
        self._wav_file.setsampwidth(2)  # 16-bit
        self._wav_file.setframerate(self._sample_rate)

        # Spawn the header-refresh worker. Uses its own r+b handle so the
        # seek never touches the wave module's writer (which sits on a
        # separate fd). Two handles are safe: the worker only touches bytes
        # 0-44 (header); wave.writeframes only appends to the data section.
        # See _header_refresh_loop for the durability argument.
        self._header_refresh_stop.clear()
        self._header_refresh_thread = threading.Thread(
            target=self._header_refresh_loop,
            name="wav-header-refresh",
            daemon=True,
        )
        self._header_refresh_thread.start()

        # Open parallel OGG Vorbis file for processed + compressed copy
        self._ogg_file = None
        self._ogg_path = None
        self._ogg_queue = None
        self._ogg_thread = None
        self._ogg_dropped_chunks = 0
        self._ogg_is_complete = True
        if _HAS_SOUNDFILE:
            try:
                self._ogg_path = self._file_path.with_suffix(".ogg")
                self._ogg_file = sf.SoundFile(
                    str(self._ogg_path), mode="w",
                    samplerate=self._sample_rate, channels=self._channels,
                    format="OGG", subtype="VORBIS",
                )
                # Start background thread for OGG processing
                self._ogg_queue = queue.Queue(maxsize=100)
                self._ogg_thread = threading.Thread(
                    target=self._ogg_writer_loop,
                    args=(self._ogg_file,),
                    daemon=True,
                )
                self._ogg_thread.start()
                logger.info("Parallel OGG compression enabled: %s", self._ogg_path)
            except Exception as e:
                logger.warning("Could not open OGG file, will compress after: %s", e)
                self._ogg_file = None
                self._ogg_path = None

        logger.info("Recording started: %s", self._file_path)
        return self._file_path

    def write_chunk(self, pcm_data: bytes) -> None:
        """Write a PCM audio chunk to the WAV file. Thread-safe.

        WAV write is synchronous (fast). OGG processing is queued to a
        background thread so RNNoise never blocks capture callbacks.
        """
        with self._lock:
            if self._wav_file and pcm_data:
                self._wav_file.writeframes(pcm_data)  # Raw to WAV (fast)

        # Queue for background OGG processing (non-blocking)
        if self._ogg_queue is not None and pcm_data:
            try:
                self._ogg_queue.put_nowait(pcm_data)
            except queue.Full:
                self._ogg_dropped_chunks += 1
                self._ogg_is_complete = False
                # Log first drop, then every 10th to avoid spam
                if self._ogg_dropped_chunks == 1 or self._ogg_dropped_chunks % 10 == 0:
                    logger.warning(
                        "OGG queue full, dropping chunk (total dropped: %d)",
                        self._ogg_dropped_chunks,
                    )

    def _header_refresh_loop(self) -> None:
        """Rewrite WAV header every 30s so a crash leaves a playable file.

        Durability argument: an earlier version tracked _total_frames in
        memory and wrote the header via a second handle, but a crash could
        leave a header claiming more audio than had actually been persisted
        (Python's userspace buffer on the wave writer's fd was not yet in
        the kernel). This version:

          1. Flushes the wave writer's Python buffer into the kernel
             (briefly under _lock so it doesn't race with writeframes).
          2. Reads the authoritative on-disk data length via
             os.fstat(handle.fileno()).st_size — once the kernel has the
             bytes, any handle referencing the same inode sees the new
             size. The header can never claim more bytes than have actually
             left userspace.
          3. Writes the header on our handle, flushes, and fsyncs.
             fsync on any fd for the inode persists all dirty pages for
             the file on both Linux and Windows (FlushFileBuffers /
             sync_file_range), so one fsync covers both the header bytes
             we just wrote and any audio bytes already in the kernel.
        """
        if self._file_path is None:
            return
        try:
            handle = open(self._file_path, "r+b")
        except OSError as e:
            logger.warning("Header refresh: could not open handle: %s", e)
            return
        try:
            while not self._header_refresh_stop.wait(_WAV_HEADER_REFRESH_SECONDS):
                # Push the wave writer's Python buffer into the kernel so
                # the file size reflects everything the recorder has
                # committed. _lock is held only for this flush call.
                with self._lock:
                    if self._wav_file is None:
                        continue  # recorder stopped between ticks
                    try:
                        self._wav_file._file.flush()
                    except Exception as e:
                        logger.debug("wav flush during header refresh: %s", e)
                        continue

                try:
                    total_size = os.fstat(handle.fileno()).st_size
                except OSError as e:
                    logger.warning("Header refresh fstat failed: %s", e)
                    continue

                if total_size < 44:
                    continue  # wave header not yet emitted
                data_size = total_size - 44
                if data_size > _WAV_HEADER_SIZE_LIMIT:
                    logger.warning(
                        "Recording exceeds 4GB WAV header limit; skipping refresh"
                    )
                    continue

                riff_size = 36 + data_size
                try:
                    handle.seek(4)
                    handle.write(riff_size.to_bytes(4, "little"))
                    handle.seek(40)
                    handle.write(data_size.to_bytes(4, "little"))
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError as e:
                    logger.warning("WAV header refresh failed: %s", e)
        finally:
            try:
                handle.close()
            except OSError:
                pass

    def _ogg_writer_loop(self, ogg_file) -> None:
        """Background thread: process and write OGG chunks.

        Applies noise suppression + AGC to mic channel before writing.
        Runs until a sentinel (None) is received or an error occurs.
        """
        while True:
            pcm_data = self._ogg_queue.get()
            try:
                if pcm_data is _SENTINEL:
                    return

                if ogg_file is None:
                    continue  # OGG disabled due to earlier error

                samples = np.frombuffer(pcm_data, dtype=np.int16)

                if self._channels >= 2:
                    samples = samples.reshape(-1, 2)
                    mic = samples[:, 0].copy()
                    loopback = samples[:, 1]

                    if self._noise_suppressor is not None:
                        mic = self._noise_suppressor.process(mic)
                    if self._agc is not None:
                        mic = self._agc.process(mic)

                    processed = np.column_stack([mic, loopback])
                else:
                    if self._noise_suppressor is not None:
                        samples = self._noise_suppressor.process(samples)
                    if self._agc is not None:
                        samples = self._agc.process(samples)
                    processed = samples

                ogg_file.write(processed)
            except Exception as e:
                logger.warning("OGG write failed, disabling parallel compression: %s", e)
                self._ogg_is_complete = False
                ogg_file = None
            finally:
                self._ogg_queue.task_done()

    def stop(self) -> Optional[Path]:
        """Stop recording and close the WAV file. Returns the file path."""
        # Stop the header-refresh worker BEFORE closing wav_file so the worker
        # never races with wave.close()'s final _patchheader write.
        self._header_refresh_stop.set()
        if self._header_refresh_thread is not None:
            self._header_refresh_thread.join(timeout=2.0)
            if self._header_refresh_thread.is_alive():
                logger.warning("WAV header refresh thread did not exit in time")
            self._header_refresh_thread = None

        with self._lock:
            if self._wav_file:
                self._wav_file.close()
                self._wav_file = None
                logger.info("Recording saved: %s", self._file_path)

        if self._ogg_dropped_chunks > 0:
            logger.warning("OGG recording: %d chunks dropped total during session", self._ogg_dropped_chunks)

        if self._ogg_queue is not None:
            try:
                self._ogg_queue.put(_SENTINEL, block=True, timeout=5)
            except queue.Full:
                logger.warning("Could not deliver OGG sentinel; writer thread may not exit cleanly")
            else:
                self._ogg_queue.join()

        if self._ogg_thread is not None:
            self._ogg_thread.join(timeout=5)
            if self._ogg_thread.is_alive():
                logger.warning("OGG writer thread did not finish in time")
            self._ogg_thread = None

        ogg_file = self._ogg_file
        self._ogg_file = None
        self._ogg_queue = None
        if ogg_file is not None:
            try:
                ogg_file.close()
                if self._ogg_path and self._ogg_path.exists() and self._file_path:
                    ogg_size = self._ogg_path.stat().st_size
                    wav_size = self._file_path.stat().st_size
                    reduction = (1 - ogg_size / wav_size) * 100 if wav_size else 0
                    logger.info(
                        "OGG ready: %s (%.0f%% smaller: %.1f MB → %.1f MB)",
                        self._ogg_path.name, reduction,
                        wav_size / 1e6, ogg_size / 1e6,
                    )
            except Exception as e:
                logger.warning("Failed to finalize OGG file: %s", e)
                self._ogg_path = None

        if not self._ogg_is_complete and self._ogg_path and self._ogg_path.exists():
            try:
                self._ogg_path.unlink()
                logger.warning("Discarded incomplete OGG fallback after dropped chunks")
            except OSError as e:
                logger.warning("Could not delete incomplete OGG fallback: %s", e)
            finally:
                self._ogg_path = None

        return self._file_path

    @property
    def compressed_path(self) -> Optional[Path]:
        """Path to the pre-compressed OGG file, if available."""
        if self._ogg_is_complete and self._ogg_path and self._ogg_path.exists():
            return self._ogg_path
        return None

    @property
    def file_path(self) -> Optional[Path]:
        return self._file_path

    @property
    def is_recording(self) -> bool:
        return self._wav_file is not None
