"""Scan audio devices to detect which are actively producing audio."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import numpy as np
import sounddevice as sd

from audio.capture import AudioDevice, suppress_portaudio_output
from audio.level_monitor import calculate_rms

logger = logging.getLogger(__name__)

# Minimum RMS to consider a device "active"
ACTIVITY_THRESHOLD = 0.005

# How long to listen to each device (seconds)
SCAN_DURATION = 0.3

# Sample rate for scanning
SCAN_SAMPLE_RATE = 16000


@dataclass
class DeviceScanResult:
    """Result of scanning a single audio device."""
    device_index: int
    device_name: str
    peak_rms: float
    is_active: bool
    scan_failed: bool

    def to_dict(self) -> dict:
        return {
            "device_index": self.device_index,
            "device_name": self.device_name,
            "peak_rms": round(self.peak_rms, 4),
            "is_active": self.is_active,
            "scan_failed": self.scan_failed,
        }


@dataclass
class ScanReport:
    """Complete scan results for all device types."""
    microphones: List[DeviceScanResult]
    loopbacks: List[DeviceScanResult]

    def to_dict(self) -> dict:
        return {
            "microphones": [r.to_dict() for r in self.microphones],
            "loopbacks": [r.to_dict() for r in self.loopbacks],
        }


def scan_microphone(device: AudioDevice, duration: float = SCAN_DURATION) -> DeviceScanResult:
    """Scan a single microphone device for audio activity."""
    peak_rms = 0.0
    samples_needed = int(SCAN_SAMPLE_RATE * duration)
    collected = 0
    event = threading.Event()

    def callback(indata: np.ndarray, frames: int, time_info, status):
        nonlocal peak_rms, collected
        rms = calculate_rms(indata[:, 0])
        if rms > peak_rms:
            peak_rms = rms
        collected += frames
        if collected >= samples_needed:
            event.set()

    try:
        stream = sd.InputStream(
            device=device.index,
            samplerate=SCAN_SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=1600,
            callback=callback,
        )
        stream.start()
        event.wait(timeout=duration + 0.5)
        stream.stop()
        stream.close()

        return DeviceScanResult(
            device_index=device.index,
            device_name=device.name,
            peak_rms=peak_rms,
            is_active=peak_rms > ACTIVITY_THRESHOLD,
            scan_failed=False,
        )
    except Exception as e:
        logger.debug("Failed to scan mic device %d (%s): %s", device.index, device.name, e)
        return DeviceScanResult(
            device_index=device.index,
            device_name=device.name,
            peak_rms=0.0,
            is_active=False,
            scan_failed=True,
        )


def _scan_loopbacks_sounddevice(
    devices: List[AudioDevice],
    duration: float = SCAN_DURATION,
    cancel: Optional[threading.Event] = None,
) -> List[DeviceScanResult]:
    """Scan loopback devices using sounddevice (cross-platform, macOS/Linux).

    Uses the same callback-stream approach as mic scanning but handles
    multiple devices concurrently via ThreadPoolExecutor.
    """
    if not devices:
        return []

    def _scan_one(device: AudioDevice) -> DeviceScanResult:
        peak_rms = 0.0
        samples_needed = int(SCAN_SAMPLE_RATE * duration)
        collected = 0
        event = threading.Event()

        def callback(indata: np.ndarray, frames: int, time_info, status):
            nonlocal peak_rms, collected
            rms = calculate_rms(indata[:, 0])
            if rms > peak_rms:
                peak_rms = rms
            collected += frames
            if collected >= samples_needed:
                event.set()

        try:
            stream = sd.InputStream(
                device=device.index,
                samplerate=SCAN_SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=1600,
                callback=callback,
            )
            stream.start()
            event.wait(timeout=duration + 0.5)
            stream.stop()
            stream.close()
            return DeviceScanResult(
                device_index=device.index,
                device_name=device.name,
                peak_rms=peak_rms,
                is_active=peak_rms > ACTIVITY_THRESHOLD,
                scan_failed=False,
            )
        except Exception as e:
            logger.debug("Failed to scan loopback %d (%s): %s", device.index, device.name, e)
            return DeviceScanResult(
                device_index=device.index,
                device_name=device.name,
                peak_rms=0.0,
                is_active=False,
                scan_failed=True,
            )

    results = []
    with ThreadPoolExecutor(max_workers=min(4, len(devices))) as executor:
        futures = {executor.submit(_scan_one, d): d for d in devices}
        for future in as_completed(futures):
            if cancel and cancel.is_set():
                break
            results.append(future.result())
    return results


def _scan_loopbacks_wasapi(
    devices: List[AudioDevice],
    duration: float = SCAN_DURATION,
    cancel: Optional[threading.Event] = None,
) -> List[DeviceScanResult]:
    """Scan loopback devices using PyAudioWPatch (Windows WASAPI only).

    Uses callback-based streams to avoid blocking on silent devices
    (WASAPI loopback read() blocks indefinitely when no audio is playing).
    All streams share one PyAudio instance to avoid segfaults from concurrent init.

    If cancel is set, streams are closed immediately and partial results returned.
    """
    if not devices:
        return []

    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return [
            DeviceScanResult(d.index, d.name, 0.0, False, True) for d in devices
        ]

    results: Dict[int, float] = {}  # device_index -> peak_rms
    streams = []

    with suppress_portaudio_output():
        p = pyaudio.PyAudio()
    try:
        for device in devices:
            if cancel and cancel.is_set():
                break
            try:
                dev_info = p.get_device_info_by_index(device.index)
                native_rate = int(dev_info["defaultSampleRate"])
                native_channels = dev_info["maxInputChannels"]
                chunk_size = int(native_rate * 0.1)

                # Closure to capture per-device state
                results[device.index] = 0.0

                def make_callback(dev_idx, n_channels):
                    def callback(in_data, frame_count, time_info, status):
                        samples = np.frombuffer(in_data, dtype=np.int16)
                        if n_channels > 1:
                            samples = samples.reshape(-1, n_channels).mean(axis=1).astype(np.int16)
                        rms = calculate_rms(samples.astype(np.float32) / 32767.0)
                        if rms > results[dev_idx]:
                            results[dev_idx] = rms
                        return (None, pyaudio.paContinue)
                    return callback

                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=native_channels,
                    rate=native_rate,
                    input=True,
                    input_device_index=device.index,
                    frames_per_buffer=chunk_size,
                    stream_callback=make_callback(device.index, native_channels),
                )
                stream.start_stream()
                streams.append(stream)
            except Exception as e:
                logger.debug("Failed to open loopback %d (%s): %s", device.index, device.name, e)
                results[device.index] = -1.0  # mark as failed

        # Let all streams collect data (interruptible via cancel event)
        if cancel:
            cancel.wait(timeout=duration)
        else:
            threading.Event().wait(timeout=duration)

        # Close all streams
        for stream in streams:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
    finally:
        p.terminate()

    # Build results
    scan_results = []
    for device in devices:
        peak = results.get(device.index, -1.0)
        failed = peak < 0
        peak = max(0.0, peak)
        scan_results.append(DeviceScanResult(
            device_index=device.index,
            device_name=device.name,
            peak_rms=peak,
            is_active=peak > ACTIVITY_THRESHOLD,
            scan_failed=failed,
        ))
    return scan_results



def scan_loopbacks(
    devices: List[AudioDevice],
    duration: float = SCAN_DURATION,
    cancel: Optional[threading.Event] = None,
) -> List[DeviceScanResult]:
    """Scan loopback devices for audio activity.

    Uses WASAPI-based scanning on Windows (handles blocking loopback reads),
    sounddevice-based scanning on macOS/Linux.
    """
    import sys
    if sys.platform == "win32":
        return _scan_loopbacks_wasapi(devices, duration, cancel)
    return _scan_loopbacks_sounddevice(devices, duration, cancel)


def scan_all_devices(
    mic_devices: List[AudioDevice],
    loopback_devices: List[AudioDevice],
    skip_indices: Optional[Set[int]] = None,
    duration: float = SCAN_DURATION,
    cancel: Optional[threading.Event] = None,
) -> ScanReport:
    """Scan all provided devices and return a ScanReport.

    Mics are scanned in parallel via ThreadPoolExecutor.
    Loopbacks are scanned simultaneously via a single shared PyAudio instance.
    If cancel event is set, scanning stops early and streams are released.
    """
    skip = skip_indices or set()
    mics_to_scan = [d for d in mic_devices if d.index not in skip]
    loopbacks_to_scan = [d for d in loopback_devices if d.index not in skip]

    if cancel and cancel.is_set():
        return ScanReport(microphones=[], loopbacks=[])

    # Run mic scans and loopback scans concurrently
    # (sounddevice + PyAudioWPatch safely coexist - the recording already does this)
    mic_results: List[DeviceScanResult] = []
    loopback_results: List[DeviceScanResult] = []

    def do_loopback_scan():
        return scan_loopbacks(loopbacks_to_scan, duration, cancel)

    with ThreadPoolExecutor(max_workers=min(4, len(mics_to_scan)) + 1) as executor:
        # Submit loopback scan as one task
        loopback_future = executor.submit(do_loopback_scan) if loopbacks_to_scan else None

        # Submit individual mic scans
        mic_futures = {
            executor.submit(scan_microphone, dev, duration): dev
            for dev in mics_to_scan
        }

        for future in as_completed(mic_futures):
            mic_results.append(future.result())

        if loopback_future:
            loopback_results = loopback_future.result()

    return ScanReport(microphones=mic_results, loopbacks=loopback_results)
