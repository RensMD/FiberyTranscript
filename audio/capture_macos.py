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

    def get_default_input_device(self) -> Optional[AudioDevice]:
        """Return the OS-default input device via sounddevice, or None.

        Skips BlackHole if it is the default input (we want a real mic here —
        BlackHole shows up as an input but is only meaningful as a loopback).
        """
        try:
            try:
                default_idx = sd.default.device[0]
            except (TypeError, IndexError):
                default_idx = sd.default.device
            if default_idx is None or default_idx < 0:
                return None
            dev = sd.query_devices(default_idx)
            if dev["max_input_channels"] <= 0:
                return None
            if "blackhole" in dev["name"].lower():
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
        """Return the first BlackHole device as the loopback default, or None.

        macOS has no native loopback default — BlackHole must be installed and
        configured as a multi-output device. Rather than trusting the OS default
        (which is typically the speakers, not a loopback), return the first
        BlackHole device found. Returns None if BlackHole is not installed.
        """
        try:
            devices = self.list_loopback_devices()
            return devices[0] if devices else None
        except Exception as e:
            logger.warning("Failed to resolve default loopback device: %s", e)
            return None

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
        # Device-disconnect detection and gap tracking are not yet wired up
        # on macOS. Accept the parameters so the interface matches Windows.
        _ = on_device_lost
        _ = on_gap
        if self._capturing:
            return
        self._capturing = True

        try:
            if mic_device:
                def mic_cb(indata, frames, time_info, status):
                    samples = (indata[:, 0] * 32767).astype(np.int16)
                    raw_level = calculate_rms(indata[:, 0])
                    if noise_suppressor:
                        cleaned = noise_suppressor.process(samples)
                        level = calculate_rms(cleaned.astype(np.float32) / 32767.0)
                    else:
                        level = raw_level
                    pcm = samples.tobytes()
                    on_level_update(level, -1, raw_level)
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
                    raise RuntimeError(
                        f"Failed to open microphone {mic_device.name!r}: {e}"
                    ) from e

            if loopback_device:
                def loopback_cb(indata, frames, time_info, status):
                    pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
                    level = calculate_rms(indata[:, 0])
                    on_level_update(-1, level, -1)
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
                    raise RuntimeError(
                        f"Failed to open loopback {loopback_device.name!r}: {e}"
                    ) from e
        except Exception:
            try:
                self.stop_capture()
            except Exception:
                logger.debug("stop_capture cleanup after failed start_capture raised",
                             exc_info=True)
            raise

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
