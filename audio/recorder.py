"""WAV file recorder for saving audio during capture sessions.

Optionally writes a compressed OGG Vorbis copy in parallel during recording,
so no post-recording compression step is needed before upload.
"""

import logging
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from config.constants import SAMPLE_RATE

logger = logging.getLogger(__name__)

# Check for soundfile (OGG Vorbis support) at import time
try:
    import soundfile as sf
    _HAS_SOUNDFILE = True
except ImportError:
    _HAS_SOUNDFILE = False


class WavRecorder:
    """Records PCM audio chunks to a WAV file.

    When soundfile is available, also writes a compressed OGG Vorbis file
    in parallel. This eliminates the post-recording compression step and
    dramatically reduces upload size for long recordings.
    """

    def __init__(self, output_dir: Path, sample_rate: int = SAMPLE_RATE):
        self._output_dir = output_dir
        self._sample_rate = sample_rate
        self._wav_file: Optional[wave.Wave_write] = None
        self._ogg_file = None  # sf.SoundFile or None
        self._file_path: Optional[Path] = None
        self._ogg_path: Optional[Path] = None
        self._lock = threading.Lock()

    def start(self) -> Path:
        """Start recording to a new WAV file. Returns the file path."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._file_path = self._output_dir / f"recording_{timestamp}.wav"

        self._wav_file = wave.open(str(self._file_path), "wb")
        self._wav_file.setnchannels(1)
        self._wav_file.setsampwidth(2)  # 16-bit
        self._wav_file.setframerate(self._sample_rate)

        # Open parallel OGG Vorbis file for compressed copy
        self._ogg_file = None
        self._ogg_path = None
        if _HAS_SOUNDFILE:
            try:
                self._ogg_path = self._file_path.with_suffix(".ogg")
                self._ogg_file = sf.SoundFile(
                    str(self._ogg_path), mode="w",
                    samplerate=self._sample_rate, channels=1,
                    format="OGG", subtype="VORBIS",
                )
                logger.info("Parallel OGG compression enabled: %s", self._ogg_path)
            except Exception as e:
                logger.warning("Could not open OGG file, will compress after: %s", e)
                self._ogg_file = None
                self._ogg_path = None

        logger.info("Recording started: %s", self._file_path)
        return self._file_path

    def write_chunk(self, pcm_data: bytes) -> None:
        """Write a PCM audio chunk to the WAV file. Thread-safe."""
        with self._lock:
            if self._wav_file and pcm_data:
                self._wav_file.writeframes(pcm_data)

                # Write compressed copy in parallel
                if self._ogg_file is not None:
                    try:
                        samples = np.frombuffer(pcm_data, dtype=np.int16)
                        self._ogg_file.write(samples)
                    except Exception as e:
                        logger.warning("OGG write failed, disabling parallel compression: %s", e)
                        self._ogg_file = None
                        self._ogg_path = None

    def stop(self) -> Optional[Path]:
        """Stop recording and close the WAV file. Returns the file path."""
        with self._lock:
            if self._wav_file:
                self._wav_file.close()
                self._wav_file = None
                logger.info("Recording saved: %s", self._file_path)

            # Close OGG file
            if self._ogg_file is not None:
                try:
                    self._ogg_file.close()
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
                finally:
                    self._ogg_file = None

            return self._file_path

    @property
    def compressed_path(self) -> Optional[Path]:
        """Path to the pre-compressed OGG file, if available."""
        if self._ogg_path and self._ogg_path.exists():
            return self._ogg_path
        return None

    @property
    def file_path(self) -> Optional[Path]:
        return self._file_path

    @property
    def is_recording(self) -> bool:
        return self._wav_file is not None
