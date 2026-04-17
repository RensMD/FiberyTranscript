"""Abstract audio capture interface and platform factory."""

import os
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, List, Optional


@contextmanager
def suppress_portaudio_output():
    """Redirect C-level stdout/stderr to devnull during PortAudio init.

    PyAudio (PortAudio) prints device names and internal IDs directly to
    the C stdout/stderr file descriptors during Pa_Initialize. Python-level
    sys.stdout redirection does not intercept these. We dup the real fds
    to devnull for the duration of the block.
    """
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        yield
        return
    old_stdout_fd = os.dup(1)
    old_stderr_fd = os.dup(2)
    try:
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        os.dup2(old_stdout_fd, 1)
        os.dup2(old_stderr_fd, 2)
        os.close(devnull_fd)
        os.close(old_stdout_fd)
        os.close(old_stderr_fd)


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
    def get_default_input_device(self) -> Optional[AudioDevice]:
        """Return the OS-default input (mic) device, or None if unavailable.

        Must NOT raise; on any error or when the OS has no default, return None
        so callers can surface a toast instead of recording from a surprise device.
        """

    @abstractmethod
    def get_default_loopback_device(self) -> Optional[AudioDevice]:
        """Return the OS-default system/loopback device, or None if unavailable.

        Same no-raise contract as get_default_input_device().
        """

    @abstractmethod
    def start_capture(
        self,
        mic_device: Optional[AudioDevice],
        loopback_device: Optional[AudioDevice],
        on_audio_chunk: Callable[[bytes, bytes], None],
        on_level_update: Callable[[float, float, float], None],
        sample_rate: int = 16000,
        noise_suppressor=None,
    ) -> None:
        """Start capturing audio.

        Args:
            mic_device: Microphone to capture from (None to skip).
            loopback_device: System audio loopback device (None to skip).
            on_audio_chunk: Callback with (mic_pcm_bytes, loopback_pcm_bytes).
            on_level_update: Callback with (mic_rms, sys_rms, raw_mic_rms). -1 = no update.
            sample_rate: Target sample rate (default 16kHz for AssemblyAI).
            noise_suppressor: Optional NoiseSuppressor for level monitoring.
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
