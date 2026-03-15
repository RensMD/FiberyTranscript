"""macOS audio capture using sounddevice (mic) and BlackHole (system audio)."""

import logging
from typing import Callable, List, Optional

import numpy as np
import sounddevice as sd

from audio.capture import AudioCapture, AudioDevice
from audio.level_monitor import calculate_rms
from config.constants import CHUNK_SAMPLES, SAMPLE_RATE

logger = logging.getLogger(__name__)


class MacOSAudioCapture(AudioCapture):
    """Audio capture for macOS. System audio requires BlackHole virtual driver."""

    def __init__(self):
        self._capturing = False
        self._mic_stream: Optional[sd.InputStream] = None
        self._loopback_stream: Optional[sd.InputStream] = None

    def list_input_devices(self) -> List[AudioDevice]:
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                # Skip BlackHole devices from mic list
                if "blackhole" in name.lower():
                    continue
                devices.append(AudioDevice(
                    index=i,
                    name=name,
                    is_input=True,
                    is_loopback=False,
                    sample_rate=int(dev["default_samplerate"]),
                    channels=dev["max_input_channels"],
                ))
        return devices

    def list_loopback_devices(self) -> List[AudioDevice]:
        """List BlackHole virtual audio devices for system audio capture."""
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                if "blackhole" in name.lower():
                    devices.append(AudioDevice(
                        index=i,
                        name=name + " (System Audio)",
                        is_input=True,
                        is_loopback=True,
                        sample_rate=int(dev["default_samplerate"]),
                        channels=dev["max_input_channels"],
                    ))
        if not devices:
            logger.warning(
                "No BlackHole devices found. Install BlackHole for system audio capture: "
                "https://existential.audio/blackhole/"
            )
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
            return
        self._capturing = True

        if mic_device:
            def mic_cb(indata, frames, time_info, status):
                pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
                level = calculate_rms(indata[:, 0])
                on_level_update(level, -1)
                on_audio_chunk(pcm, b"")

            try:
                self._mic_stream = sd.InputStream(
                    device=mic_device.index,
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=CHUNK_SAMPLES,
                    callback=mic_cb,
                )
                self._mic_stream.start()
                logger.info("Microphone capture started: %s", mic_device.name)
            except Exception as e:
                logger.error("Failed to open microphone %s: %s", mic_device.name, e)
                self._mic_stream = None

        if loopback_device:
            def loopback_cb(indata, frames, time_info, status):
                pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
                level = calculate_rms(indata[:, 0])
                on_level_update(-1, level)
                on_audio_chunk(b"", pcm)

            try:
                self._loopback_stream = sd.InputStream(
                    device=loopback_device.index,
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=CHUNK_SAMPLES,
                    callback=loopback_cb,
                )
                self._loopback_stream.start()
                logger.info("Loopback capture started: %s", loopback_device.name)
            except Exception as e:
                logger.error("Failed to open loopback %s: %s", loopback_device.name, e)
                self._loopback_stream = None

    def stop_capture(self) -> None:
        self._capturing = False
        if self._mic_stream:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception as e:
                logger.warning("Error closing mic stream: %s", e)
            self._mic_stream = None
        if self._loopback_stream:
            try:
                self._loopback_stream.stop()
                self._loopback_stream.close()
            except Exception as e:
                logger.warning("Error closing loopback stream: %s", e)
            self._loopback_stream = None

    def is_capturing(self) -> bool:
        return self._capturing
