import shutil
import wave
from pathlib import Path

import pytest

from audio.wav_merge import merge_wav_files


def _make_test_root(name: str) -> Path:
    root = Path.cwd() / "data" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_wav(path: Path, channels: int) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00" * 3200 * channels)


def test_merge_rejects_mismatched_wav_formats():
    root = _make_test_root("test_wav_merge_format_validation")
    try:
        mono = root / "mono.wav"
        stereo = root / "stereo.wav"
        _write_wav(mono, channels=1)
        _write_wav(stereo, channels=2)

        with pytest.raises(ValueError, match="different formats"):
            merge_wav_files([mono, stereo])
    finally:
        shutil.rmtree(root, ignore_errors=True)
