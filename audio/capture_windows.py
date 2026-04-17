"""Windows audio capture using PyAudioWPatch (WASAPI loopback) and sounddevice."""

import logging
import queue
import threading
import time
from typing import Callable, List, Optional

import numpy as np
import sounddevice as sd

from audio.capture import AudioCapture, AudioDevice, suppress_portaudio_output
from audio.level_monitor import calculate_rms
from config.constants import CHUNK_SAMPLES, SAMPLE_RATE

logger = logging.getLogger(__name__)

_LOOPBACK_QUEUE_TIMEOUT_SECONDS = 0.1
_LOOPBACK_STALL_TIMEOUT_SECONDS = 0.2
_LOOPBACK_SILENCE_CHUNK_BYTES = CHUNK_SAMPLES * 2
# After this many seconds of continuous silence injection, escalate to
# on_device_lost rather than padding silence forever. User learns about
# the dead channel during the meeting, not after.
_LOOPBACK_STALL_ESCALATION_SECONDS = 10.0


class _LoopbackStallWatchdog:
    """Tracks loopback callback starvation and silence injection cadence."""

    def __init__(
        self,
        stall_timeout_seconds: float = _LOOPBACK_STALL_TIMEOUT_SECONDS,
        emit_interval_seconds: float = CHUNK_SAMPLES / SAMPLE_RATE,
        start_time: float | None = None,
    ):
        self._stall_timeout_seconds = stall_timeout_seconds
        self._emit_interval_seconds = emit_interval_seconds
        start = time.monotonic() if start_time is None else start_time
        self._last_data_time = start
        self._stall_started_time: float | None = None
        self._last_silence_emit_time: float | None = None
        self.stall_count = 0
        self.longest_stall_seconds = 0.0

    def poll_timeout(self, now: float) -> tuple[bool, bool]:
        """Return (entered_stall, emit_silence) for the current timeout tick."""
        if (now - self._last_data_time) < self._stall_timeout_seconds:
            return False, False

        entered_stall = False
        if self._stall_started_time is None:
            self._stall_started_time = now
            self._last_silence_emit_time = None
            self.stall_count += 1
            entered_stall = True

        should_emit = (
            self._last_silence_emit_time is None
            or (now - self._last_silence_emit_time) >= self._emit_interval_seconds
        )
        if should_emit:
            self._last_silence_emit_time = now
        return entered_stall, should_emit

    def notify_data(self, now: float) -> float | None:
        """Record loopback data arrival and return recovery duration if applicable."""
        recovery_duration = None
        if self._stall_started_time is not None:
            recovery_duration = max(0.0, now - self._stall_started_time)
            self.longest_stall_seconds = max(self.longest_stall_seconds, recovery_duration)
            self._stall_started_time = None
            self._last_silence_emit_time = None
        self._last_data_time = now
        return recovery_duration

    def finalize(self, now: float) -> float | None:
        """Close out any active stall so summaries include the final gap."""
        if self._stall_started_time is None:
            return None
        active_duration = max(0.0, now - self._stall_started_time)
        self.longest_stall_seconds = max(self.longest_stall_seconds, active_duration)
        return active_duration

    def stall_duration(self, now: float) -> float:
        """Seconds since the current stall began, or 0.0 if not stalled."""
        if self._stall_started_time is None:
            return 0.0
        return max(0.0, now - self._stall_started_time)


class WindowsAudioCapture(AudioCapture):
    """Audio capture for Windows using WASAPI loopback for system audio."""

    def __init__(self):
        self._capturing = False
        self._mic_stream: Optional[sd.InputStream] = None
        self._loopback_stream = None  # PyAudio stream
        self._loopback_thread: Optional[threading.Thread] = None
        self._pyaudio_instance = None
        self._loopback_device_cache: Optional[List[AudioDevice]] = None
        # One-shot log flags for stream-status warnings. Avoids flooding the
        # log with one line per callback when a stream enters a degraded state.
        self._mic_status_warned = False
        self._loopback_status_warned = False
        # Device-disconnect detection (Phase 2.1).
        self._on_device_lost: Optional[Callable[[str, str], None]] = None
        self._mic_device_name: Optional[str] = None
        self._loopback_device_name: Optional[str] = None
        self._mic_watcher_thread: Optional[threading.Thread] = None
        self._mic_watcher_stop = threading.Event()
        # Latch so a watcher cannot double-fire on_device_lost for the same source.
        self._mic_lost_fired = False
        self._loopback_lost_fired = False
        # Gap-event callback (Phase 2.4). Optional; only wired for loopback stalls here.
        self._on_gap: Optional[Callable[[str, str, float, Optional[float]], None]] = None

    def reinitialize(self) -> None:
        """Re-initialize sounddevice to pick up newly connected devices."""
        self._loopback_device_cache = None
        try:
            sd._terminate()
            sd._initialize()
            logger.info("sounddevice re-initialized for device refresh")
        except Exception as e:
            logger.warning("Failed to re-initialize sounddevice: %s", e)

    def list_input_devices(self) -> List[AudioDevice]:
        """List microphone devices via sounddevice."""
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and dev["hostapi"] == 0:
                devices.append(AudioDevice(
                    index=i,
                    name=dev["name"],
                    is_input=True,
                    is_loopback=False,
                    sample_rate=int(dev["default_samplerate"]),
                    channels=dev["max_input_channels"],
                ))
        return devices

    def list_loopback_devices(self) -> List[AudioDevice]:
        """List WASAPI loopback devices via PyAudioWPatch.

        When a capture stream is active, returns a cached list to avoid
        creating/terminating PyAudio instances — Pa_Initialize/Pa_Terminate
        cycles during active WASAPI capture cause periodic audio glitches.
        """
        if self._capturing and self._loopback_device_cache is not None:
            return list(self._loopback_device_cache)

        devices = []
        try:
            import pyaudiowpatch as pyaudio
            with suppress_portaudio_output():
                p = pyaudio.PyAudio()
            try:
                wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                for i in range(p.get_device_count()):
                    dev = p.get_device_info_by_index(i)
                    if dev["hostApi"] != wasapi_info["index"]:
                        continue
                    if dev["maxInputChannels"] <= 0:
                        continue
                    if dev.get("isLoopbackDevice", False):
                        devices.append(AudioDevice(
                            index=i,
                            name=dev["name"],
                            is_input=True,
                            is_loopback=True,
                            sample_rate=int(dev["defaultSampleRate"]),
                            channels=dev["maxInputChannels"],
                        ))
            finally:
                p.terminate()
        except Exception as e:
            logger.warning("Failed to enumerate WASAPI loopback devices: %s", e)
            try:
                devices = self._find_loopback_fallback()
            except Exception as e2:
                logger.error("Fallback loopback enumeration also failed: %s", e2)
        self._loopback_device_cache = list(devices)
        return devices

    def get_default_input_device(self) -> Optional[AudioDevice]:
        """Return the OS-default input device via sounddevice, or None."""
        try:
            # sd.default.device is a _InputOutputPair (input, output); subscriptable.
            # -1 / None means no default.
            try:
                default_idx = sd.default.device[0]
            except (TypeError, IndexError):
                default_idx = sd.default.device
            if default_idx is None or default_idx < 0:
                return None
            dev = sd.query_devices(default_idx)
            if dev["max_input_channels"] <= 0:
                return None
            return AudioDevice(
                index=int(default_idx),
                name=dev["name"],
                is_input=True,
                is_loopback=False,
                sample_rate=int(dev["default_samplerate"]),
                channels=dev["max_input_channels"],
            )
        except Exception as e:
            logger.warning("Failed to resolve default input device: %s", e)
            return None

    def get_default_loopback_device(self) -> Optional[AudioDevice]:
        """Return the OS-default WASAPI loopback device, or None."""
        try:
            import pyaudiowpatch as pyaudio
            with suppress_portaudio_output():
                p = pyaudio.PyAudio()
            try:
                default = p.get_default_wasapi_loopback()
                if not default:
                    return None
                return AudioDevice(
                    index=int(default["index"]),
                    name=default["name"] + " (Loopback)",
                    is_input=True,
                    is_loopback=True,
                    sample_rate=int(default["defaultSampleRate"]),
                    channels=int(default["maxInputChannels"]),
                )
            finally:
                p.terminate()
        except Exception as e:
            logger.warning("Failed to resolve default loopback device: %s", e)
            return None

    def _find_loopback_fallback(self) -> List[AudioDevice]:
        """Fallback method to find loopback device from default output."""
        import pyaudiowpatch as pyaudio
        devices = []
        with suppress_portaudio_output():
            p = pyaudio.PyAudio()
        try:
            default_output = p.get_default_wasapi_loopback()
            if default_output:
                devices.append(AudioDevice(
                    index=default_output["index"],
                    name=default_output["name"] + " (Loopback)",
                    is_input=True,
                    is_loopback=True,
                    sample_rate=int(default_output["defaultSampleRate"]),
                    channels=default_output["maxInputChannels"],
                ))
        finally:
            p.terminate()
        return devices

    def start_capture(
        self,
        mic_device: Optional[AudioDevice],
        loopback_device: Optional[AudioDevice],
        on_audio_chunk: Callable[[bytes, bytes], None],
        on_level_update: Callable[[float, float, float], None],
        sample_rate: int = SAMPLE_RATE,
        noise_suppressor=None,
        on_device_lost: Optional[Callable[[str, str], None]] = None,
        on_gap: Optional[Callable[[str, str, float, Optional[float]], None]] = None,
    ) -> None:
        if self._capturing:
            logger.warning("Already capturing, call stop_capture first")
            return

        self._capturing = True
        self._on_audio_chunk = on_audio_chunk
        self._on_level_update = on_level_update
        self._noise_suppressor = noise_suppressor
        self._on_device_lost = on_device_lost
        self._on_gap = on_gap
        self._mic_device_name = mic_device.name if mic_device else None
        self._loopback_device_name = loopback_device.name if loopback_device else None
        self._mic_lost_fired = False
        self._loopback_lost_fired = False
        self._mic_watcher_stop.clear()

        # Start microphone capture via sounddevice
        if mic_device:
            self._start_mic(mic_device, sample_rate)

        # Start loopback capture via PyAudioWPatch
        if loopback_device:
            self._start_loopback(loopback_device, sample_rate)

    def _start_mic(self, device: AudioDevice, sample_rate: int) -> None:
        """Start microphone capture stream."""
        def mic_callback(indata: np.ndarray, frames: int, time_info, status):
            if status:
                if not self._mic_status_warned:
                    logger.debug("Mic stream status: %s", status)
                    self._mic_status_warned = True
            elif self._mic_status_warned:
                logger.debug("Mic stream status: recovered")
                self._mic_status_warned = False
            samples = (indata[:, 0] * 32767).astype(np.int16)
            raw_level = calculate_rms(indata[:, 0])
            # Noise suppression for speech detection only
            if self._noise_suppressor:
                cleaned = self._noise_suppressor.process(samples)
                level = calculate_rms(cleaned.astype(np.float32) / 32767.0)
            else:
                level = raw_level
            pcm = samples.tobytes()
            self._on_level_update(level, -1, raw_level)  # -1 means "no update for this source"
            self._on_audio_chunk(pcm, b"")

        try:
            self._mic_stream = sd.InputStream(
                device=device.index,
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=CHUNK_SAMPLES,
                callback=mic_callback,
            )
            self._mic_stream.start()
            logger.info("Microphone capture started: %s", device.name)
        except Exception as e:
            logger.error("Failed to open microphone %s: %s", device.name, e)
            self._mic_stream = None
            return

        # Spawn a dedicated watcher thread that polls stream.active. sounddevice
        # signals disconnect by silently deactivating the stream (callback just
        # stops firing), so polling is the reliable detection path. A separate
        # thread is simpler than piggy-backing on the loopback thread — mic-only
        # recordings don't have a loopback thread, and the watcher can fire
        # on_device_lost without taking any mixer / app locks.
        self._mic_watcher_thread = threading.Thread(
            target=self._mic_watcher_loop,
            args=(device.name,),
            name="mic-watcher",
            daemon=True,
        )
        self._mic_watcher_thread.start()

    def _mic_watcher_loop(self, device_name: str) -> None:
        """Poll the mic stream for unexpected deactivation (disconnect)."""
        while not self._mic_watcher_stop.wait(2.0):
            if not self._capturing:
                return
            stream = self._mic_stream
            # stream may have been torn down by stop_capture; that's graceful.
            if stream is None:
                return
            try:
                active = stream.active
            except Exception:
                active = False
            if not active:
                if self._mic_lost_fired:
                    return
                self._mic_lost_fired = True
                logger.warning("Mic stream went inactive unexpectedly: %s", device_name)
                if self._on_device_lost:
                    try:
                        self._on_device_lost("mic", device_name)
                    except Exception:
                        logger.exception("on_device_lost('mic', ...) raised")
                return

    def _start_loopback(self, device: AudioDevice, target_sample_rate: int) -> None:
        """Start WASAPI loopback capture in a background thread."""
        self._loopback_thread = threading.Thread(
            target=self._loopback_capture_loop,
            args=(device, target_sample_rate),
            daemon=True,
        )
        self._loopback_thread.start()
        logger.info("Loopback capture started: %s", device.name)

    def _loopback_capture_loop(self, device: AudioDevice, target_sample_rate: int) -> None:
        """Background thread for WASAPI loopback capture.

        Uses callback mode to avoid blocking on silent devices — WASAPI loopback
        read() blocks indefinitely when no audio is playing, causing ~4-second
        periodic cutouts in the stereo mix.

        The callback only copies raw bytes into a queue (no GIL-heavy numpy/scipy)
        so that PortAudio's audio thread is never delayed. This thread drains the
        queue and does the downmix + resample work.
        """
        import pyaudiowpatch as pyaudio

        with suppress_portaudio_output():
            p = pyaudio.PyAudio()
        self._pyaudio_instance = p
        watchdog: _LoopbackStallWatchdog | None = None
        native_rate = 0
        native_channels = 0
        chunk_size = 0

        # Register this thread for MMCSS "Pro Audio" scheduling so the Windows
        # scheduler prioritizes it under CPU load (reduces audio glitches while
        # transcription/summarization hammers the CPU). Reverted in the finally
        # block below. Falls through silently on non-Windows / unsupported runtimes.
        mmcss_handle = None
        avrt = None
        try:
            import ctypes
            avrt = ctypes.windll.LoadLibrary("avrt.dll")
            task_index = ctypes.c_ulong(0)
            mmcss_handle = avrt.AvSetMmThreadCharacteristicsW(
                "Pro Audio", ctypes.byref(task_index)
            )
            if mmcss_handle:
                logger.debug("MMCSS 'Pro Audio' registered for loopback thread")
            else:
                logger.debug("MMCSS registration returned NULL (non-critical)")
        except Exception as e:
            logger.debug("MMCSS unavailable: %s", e)

        try:
            dev_info = p.get_device_info_by_index(device.index)
            native_rate = int(dev_info["defaultSampleRate"])
            native_channels = dev_info["maxInputChannels"]

            chunk_size = int(native_rate * CHUNK_SAMPLES / target_sample_rate)

            # Pre-compute resampling parameters
            need_resample = native_rate != target_sample_rate
            resample_func = None
            rs_up = 1
            rs_down = 1
            if need_resample:
                from scipy.signal import resample_poly as _resample_poly
                from math import gcd
                g = gcd(target_sample_rate, native_rate)
                rs_up = target_sample_rate // g
                rs_down = native_rate // g
                resample_func = _resample_poly

            # Queue for raw bytes from the PortAudio callback thread.
            # The callback does zero numpy work - just a fast bytes copy.
            raw_queue: queue.Queue = queue.Queue(maxsize=200)

            def loopback_callback(in_data, frame_count, time_info, status):
                if not self._capturing:
                    return (None, pyaudio.paComplete)
                if status:
                    if not self._loopback_status_warned:
                        logger.debug("Loopback stream status: %s", status)
                        self._loopback_status_warned = True
                elif self._loopback_status_warned:
                    logger.debug("Loopback stream status: recovered")
                    self._loopback_status_warned = False
                try:
                    raw_queue.put_nowait(bytes(in_data))
                except queue.Full:
                    pass  # drop rather than block the audio thread
                return (None, pyaudio.paContinue)

            # Try opening the stream in callback mode, falling back to smaller
            # buffers for devices with limited memory (e.g. Bluetooth LE).
            stream = None
            for attempt_chunk in (chunk_size, chunk_size // 2, 512):
                try:
                    stream = p.open(
                        format=pyaudio.paInt16,
                        channels=native_channels,
                        rate=native_rate,
                        input=True,
                        input_device_index=device.index,
                        frames_per_buffer=attempt_chunk,
                        stream_callback=loopback_callback,
                    )
                    chunk_size = attempt_chunk
                    break
                except OSError as open_err:
                    logger.warning("Loopback open failed with buffer %d: %s", attempt_chunk, open_err)

            if stream is None:
                logger.error("Could not open loopback stream for %s", device.name)
                return

            self._loopback_stream = stream
            stream.start_stream()
            logger.info(
                "Loopback capture config: device=%s native_rate=%d channels=%d frames_per_buffer=%d",
                device.name,
                native_rate,
                native_channels,
                chunk_size,
            )
            watchdog = _LoopbackStallWatchdog(
                emit_interval_seconds=CHUNK_SAMPLES / float(target_sample_rate),
                start_time=time.monotonic(),
            )
            silence_chunk = b"\x00" * _LOOPBACK_SILENCE_CHUNK_BYTES

            # Drain the queue on THIS thread - all numpy/scipy work happens here,
            # keeping the PortAudio callback thread free for timely buffer servicing.
            while self._capturing and stream.is_active():
                try:
                    data = raw_queue.get(timeout=_LOOPBACK_QUEUE_TIMEOUT_SECONDS)
                except queue.Empty:
                    now = time.monotonic()
                    entered_stall, emit_silence = watchdog.poll_timeout(now)
                    if entered_stall:
                        logger.warning(
                            "Loopback callback gap detected: device=%s native_rate=%d "
                            "channels=%d frames_per_buffer=%d; injecting silence",
                            device.name,
                            native_rate,
                            native_channels,
                            chunk_size,
                        )
                        if self._on_gap:
                            try:
                                self._on_gap("loopback", "stall", now, None)
                            except Exception:
                                logger.exception("on_gap(stall start) raised")
                    # Escalate a long, unbroken stall to a full device-lost
                    # event. Silence injection alone would hide a dead channel
                    # for the entire meeting.
                    if watchdog.stall_duration(now) > _LOOPBACK_STALL_ESCALATION_SECONDS:
                        logger.warning(
                            "Loopback stall exceeded %.0fs, escalating to device lost: %s",
                            _LOOPBACK_STALL_ESCALATION_SECONDS,
                            device.name,
                        )
                        if self._on_device_lost and not self._loopback_lost_fired:
                            self._loopback_lost_fired = True
                            try:
                                self._on_device_lost("loopback", device.name)
                            except Exception:
                                logger.exception(
                                    "on_device_lost('loopback', ...) raised during escalation"
                                )
                        break  # exit capture loop; cleanup in finally
                    if emit_silence:
                        self._on_level_update(-1, 0.0, -1)
                        self._on_audio_chunk(b"", silence_chunk)
                    continue

                recovery_time = time.monotonic()
                recovery_duration = watchdog.notify_data(recovery_time)
                if recovery_duration is not None:
                    logger.warning(
                        "Loopback callback recovered: device=%s duration=%.3fs stall_count=%d",
                        device.name,
                        recovery_duration,
                        watchdog.stall_count,
                    )
                    if self._on_gap:
                        try:
                            self._on_gap("loopback", "stall", recovery_time, recovery_duration)
                        except Exception:
                            logger.exception("on_gap(stall close) raised")

                samples = np.frombuffer(data, dtype=np.int16)
                if native_channels > 1:
                    samples = samples.reshape(-1, native_channels).mean(axis=1).astype(np.int16)

                if need_resample:
                    resampled = resample_func(samples.astype(np.float32), rs_up, rs_down)
                    samples = resampled.astype(np.int16)

                pcm = samples.tobytes()
                level = calculate_rms(samples.astype(np.float32) / 32767.0)
                self._on_level_update(-1, level, -1)
                self._on_audio_chunk(b"", pcm)

            # If the loop exited while we still intend to capture, the stream
            # died underneath us (device removed, driver fault, etc.). Raise
            # on_device_lost before teardown so the app can degrade gracefully
            # to the surviving source instead of silently losing loopback.
            if self._capturing and stream is not None:
                try:
                    still_active = stream.is_active()
                except Exception:
                    still_active = False
                if not still_active and not self._loopback_lost_fired:
                    self._loopback_lost_fired = True
                    logger.warning(
                        "Loopback stream died unexpectedly: %s", device.name
                    )
                    if self._on_device_lost:
                        try:
                            self._on_device_lost("loopback", device.name)
                        except Exception:
                            logger.exception("on_device_lost('loopback', ...) raised")

        except Exception as e:
            logger.error("Loopback capture error: %s", e)
        finally:
            if watchdog is not None:
                watchdog.finalize(time.monotonic())
                if watchdog.stall_count:
                    logger.warning(
                        "Loopback capture summary: device=%s stalls=%d longest=%.3fs "
                        "native_rate=%d channels=%d frames_per_buffer=%d",
                        device.name,
                        watchdog.stall_count,
                        watchdog.longest_stall_seconds,
                        native_rate,
                        native_channels,
                        chunk_size,
                    )
            try:
                if self._loopback_stream:
                    self._loopback_stream.stop_stream()
                    self._loopback_stream.close()
            except OSError:
                pass  # stream already closed / in bad state
            self._loopback_stream = None
            p.terminate()
            self._pyaudio_instance = None
            if mmcss_handle and avrt is not None:
                try:
                    avrt.AvRevertMmThreadCharacteristics(mmcss_handle)
                except Exception:
                    pass

    def stop_capture(self) -> None:
        self._capturing = False
        self._loopback_device_cache = None  # Allow fresh enumeration when idle
        self._mic_watcher_stop.set()

        if self._mic_stream:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception as e:
                logger.warning("Error closing mic stream: %s", e)
            self._mic_stream = None
            logger.info("Microphone capture stopped")

        if self._mic_watcher_thread is not None:
            self._mic_watcher_thread.join(timeout=3.0)
            self._mic_watcher_thread = None

        if self._loopback_thread:
            self._loopback_thread.join(timeout=3.0)
            self._loopback_thread = None
            logger.info("Loopback capture stopped")

        self._on_device_lost = None
        self._on_gap = None
        self._mic_device_name = None
        self._loopback_device_name = None

    def is_capturing(self) -> bool:
        return self._capturing
