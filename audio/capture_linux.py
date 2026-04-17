"""Linux audio capture using sounddevice with PulseAudio/PipeWire monitor devices."""

import logging
from typing import Callable, List, Optional

import numpy as np
import sounddevice as sd

from audio.capture import AudioCapture, AudioDevice
from audio.level_monitor import calculate_rms
from config.constants import CHUNK_SAMPLES, SAMPLE_RATE

logger = logging.getLogger(__name__)


class LinuxAudioCapture(AudioCapture):
    """Audio capture for Linux using PulseAudio/PipeWire monitor sources."""

    def __init__(self):
        self._capturing = False
        self._mic_stream: Optional[sd.InputStream] = None
        self._loopback_stream: Optional[sd.InputStream] = None

    def list_input_devices(self) -> List[AudioDevice]:
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                # Skip monitor devices from mic list
                if "monitor" in name.lower():
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
        """List PulseAudio/PipeWire monitor devices for system audio."""
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                if "monitor" in name.lower():
                    devices.append(AudioDevice(
                        index=i,
                        name=name,
                        is_input=True,
                        is_loopback=True,
                        sample_rate=int(dev["default_samplerate"]),
                        channels=dev["max_input_channels"],
                    ))
        return devices

    def get_default_input_device(self) -> Optional[AudioDevice]:
        """Return the OS-default input device via sounddevice, or None.

        Skips monitor devices if the default is one (we want a real mic here —
        monitor devices show up as inputs but are loopbacks).
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
            if "monitor" in dev["name"].lower():
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
        """Return the monitor source for the default output sink, or None.

        Linux (PulseAudio/PipeWire) exposes loopback as `<sink>.monitor`. We pick
        the default output, then find the matching monitor device. Falls back to
        the first enumerated monitor if the match can't be resolved.
        """
        try:
            try:
                default_out_idx = sd.default.device[1]
            except (TypeError, IndexError):
                default_out_idx = None
            default_out_name = None
            if default_out_idx is not None and default_out_idx >= 0:
                try:
                    default_out_name = sd.query_devices(default_out_idx)["name"]
                except Exception:
                    default_out_name = None

            monitors = self.list_loopback_devices()
            if not monitors:
                return None
            if default_out_name:
                # Pulse/PipeWire convention: monitor name starts with the sink name
                for mon in monitors:
                    if default_out_name in mon.name or mon.name.startswith(default_out_name):
                        return mon
            return monitors[0]
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
        # on Linux. Accept the parameters so the interface matches Windows.
        _ = on_device_lost
        _ = on_gap
        if self._capturing:
            return
        self._capturing = True

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
