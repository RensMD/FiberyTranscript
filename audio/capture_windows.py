"""Windows audio capture using PyAudioWPatch (WASAPI loopback) and sounddevice."""

import logging
import threading
from typing import Callable, List, Optional

import numpy as np
import sounddevice as sd

from audio.capture import AudioCapture, AudioDevice
from audio.level_monitor import calculate_rms
from config.constants import CHUNK_SAMPLES, SAMPLE_RATE

logger = logging.getLogger(__name__)


class WindowsAudioCapture(AudioCapture):
    """Audio capture for Windows using WASAPI loopback for system audio."""

    def __init__(self):
        self._capturing = False
        self._mic_stream: Optional[sd.InputStream] = None
        self._loopback_stream = None  # PyAudio stream
        self._loopback_thread: Optional[threading.Thread] = None
        self._pyaudio_instance = None

    def reinitialize(self) -> None:
        """Re-initialize sounddevice to pick up newly connected devices."""
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

        A fresh PyAudio instance is created and terminated on each call so
        that devices plugged in after app start are always detected.
        """
        devices = []
        try:
            import pyaudiowpatch as pyaudio
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
        return devices

    def _find_loopback_fallback(self) -> List[AudioDevice]:
        """Fallback method to find loopback device from default output."""
        import pyaudiowpatch as pyaudio
        devices = []
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
        on_level_update: Callable[[float, float], None],
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        if self._capturing:
            logger.warning("Already capturing, call stop_capture first")
            return

        self._capturing = True
        self._on_audio_chunk = on_audio_chunk
        self._on_level_update = on_level_update

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
                logger.debug("Mic stream status: %s", status)
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            level = calculate_rms(indata[:, 0])
            self._on_level_update(level, -1)  # -1 means "no update for this source"
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
        """Background thread for WASAPI loopback capture."""
        import pyaudiowpatch as pyaudio

        p = pyaudio.PyAudio()
        self._pyaudio_instance = p

        try:
            dev_info = p.get_device_info_by_index(device.index)
            native_rate = int(dev_info["defaultSampleRate"])
            native_channels = dev_info["maxInputChannels"]

            chunk_size = int(native_rate * CHUNK_SAMPLES / target_sample_rate)

            # Try opening the stream, falling back to smaller buffers for
            # devices with limited memory (e.g. Bluetooth LE).
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
                    )
                    chunk_size = attempt_chunk
                    break
                except OSError as open_err:
                    logger.warning("Loopback open failed with buffer %d: %s", attempt_chunk, open_err)

            if stream is None:
                logger.error("Could not open loopback stream for %s", device.name)
                return

            self._loopback_stream = stream

            while self._capturing:
                try:
                    data = stream.read(chunk_size, exception_on_overflow=False)
                except Exception:
                    if not self._capturing:
                        break
                    continue

                # Convert to numpy, downmix to mono, resample to target rate
                samples = np.frombuffer(data, dtype=np.int16)
                if native_channels > 1:
                    samples = samples.reshape(-1, native_channels).mean(axis=1).astype(np.int16)

                # Resample if rates differ (polyphase with anti-aliasing)
                if native_rate != target_sample_rate:
                    from scipy.signal import resample_poly
                    from math import gcd
                    g = gcd(target_sample_rate, native_rate)
                    up = target_sample_rate // g
                    down = native_rate // g
                    resampled = resample_poly(samples.astype(np.float32), up, down)
                    samples = resampled.astype(np.int16)

                pcm = samples.tobytes()
                level = calculate_rms(samples.astype(np.float32) / 32767.0)
                self._on_level_update(-1, level)
                self._on_audio_chunk(b"", pcm)

        except Exception as e:
            logger.error("Loopback capture error: %s", e)
        finally:
            try:
                if self._loopback_stream:
                    self._loopback_stream.stop_stream()
                    self._loopback_stream.close()
            except OSError:
                pass  # stream already closed / in bad state
            self._loopback_stream = None
            p.terminate()
            self._pyaudio_instance = None

    def stop_capture(self) -> None:
        self._capturing = False

        if self._mic_stream:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception as e:
                logger.warning("Error closing mic stream: %s", e)
            self._mic_stream = None
            logger.info("Microphone capture stopped")

        if self._loopback_thread:
            self._loopback_thread.join(timeout=3.0)
            self._loopback_thread = None
            logger.info("Loopback capture stopped")

    def is_capturing(self) -> bool:
        return self._capturing
