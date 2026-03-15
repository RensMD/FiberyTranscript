"""Abstract audio capture interface and platform factory."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class AudioDevice:
    """Represents an audio input/output device."""
    index: int
    name: str
    is_input: bool       # True for mic, False for output device
    is_loopback: bool    # True for system audio loopback
    sample_rate: int
    channels: int

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "is_input": self.is_input,
            "is_loopback": self.is_loopback,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }


class AudioCapture(ABC):
    """Abstract base class for platform-specific audio capture."""

    @abstractmethod
    def list_input_devices(self) -> List[AudioDevice]:
        """List available microphone devices."""

    @abstractmethod
    def list_loopback_devices(self) -> List[AudioDevice]:
        """List available system/speaker loopback devices."""

    @abstractmethod
    def start_capture(
        self,
        mic_device: Optional[AudioDevice],
        loopback_device: Optional[AudioDevice],
        on_audio_chunk: Callable[[bytes, bytes], None],
        on_level_update: Callable[[float, float], None],
        sample_rate: int = 16000,
    ) -> None:
        """Start capturing audio.

        Args:
            mic_device: Microphone to capture from (None to skip).
            loopback_device: System audio loopback device (None to skip).
            on_audio_chunk: Callback with (mic_pcm_bytes, loopback_pcm_bytes).
            on_level_update: Callback with (mic_rms_0to1, loopback_rms_0to1).
            sample_rate: Target sample rate (default 16kHz for AssemblyAI).
        """

    @abstractmethod
    def stop_capture(self) -> None:
        """Stop all audio capture."""

    @abstractmethod
    def is_capturing(self) -> bool:
        """Return True if currently capturing audio."""

    def reinitialize(self) -> None:
        """Re-initialize audio backends to pick up newly connected devices.

        Default implementation does nothing. Override on platforms where
        the audio backend caches the device list (e.g. sounddevice).
        """


def create_audio_capture() -> AudioCapture:
    """Factory: create the appropriate AudioCapture for the current platform."""
    import sys
    if sys.platform == "win32":
        from audio.capture_windows import WindowsAudioCapture
        return WindowsAudioCapture()
    elif sys.platform == "darwin":
        from audio.capture_macos import MacOSAudioCapture
        return MacOSAudioCapture()
    else:
        from audio.capture_linux import LinuxAudioCapture
        return LinuxAudioCapture()
