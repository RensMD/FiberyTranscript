import shutil
import time
import queue as pyqueue
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from audio import recorder as recorder_module
from audio.recorder import WavRecorder
from utils.filename_utils import WINDOWS_SAFE_PATH_LIMIT


def _make_test_root(name: str) -> Path:
    root = Path.cwd() / "data" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_chunk(value: int, samples: int = 1600) -> bytes:
    return np.full(samples, value, dtype=np.int16).tobytes()


class _FakeSoundFile:
    writes = []

    def __init__(self, path, mode, samplerate, channels, format, subtype):
        self.path = Path(path)
        self.closed = False

    def write(self, samples):
        time.sleep(0.03)
        self.__class__.writes.append(samples.copy())

    def close(self):
        self.closed = True
        self.path.write_bytes(b"ogg")


def test_stop_flushes_queued_ogg_chunks_before_closing():
    root = _make_test_root("test_recorder_flush_stop")
    try:
        _FakeSoundFile.writes = []
        fake_sf = SimpleNamespace(SoundFile=_FakeSoundFile)

        with patch.object(recorder_module, "_HAS_SOUNDFILE", True):
            with patch.object(recorder_module, "sf", fake_sf, create=True):
                recorder = WavRecorder(root, channels=1)
                recorder.start()

                chunks = [_make_chunk(value) for value in range(8)]
                for chunk in chunks:
                    recorder.write_chunk(chunk)

                recorder.stop()

        assert len(_FakeSoundFile.writes) == len(chunks)
        assert recorder.compressed_path is not None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_incomplete_ogg_fallback_is_discarded_after_queue_drops():
    root = _make_test_root("test_recorder_discard_incomplete_ogg")
    try:
        _FakeSoundFile.writes = []
        fake_sf = SimpleNamespace(SoundFile=_FakeSoundFile)

        with patch.object(recorder_module, "_HAS_SOUNDFILE", True):
            with patch.object(recorder_module, "sf", fake_sf, create=True):
                with patch.object(
                    recorder_module.queue,
                    "Queue",
                    side_effect=lambda maxsize=0: pyqueue.Queue(maxsize=2),
                ):
                    recorder = WavRecorder(root, channels=1)
                    recorder.start()

                    for value in range(40):
                        recorder.write_chunk(_make_chunk(value))

                    recorder.stop()

        assert recorder.compressed_path is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_start_truncates_long_meeting_name_to_windows_safe_path():
    root = _make_test_root("test_recorder_long_name")
    try:
        output_dir = root / "deeper" / "path" / "that" / "is" / "already" / "fairly" / "long"
        recorder = WavRecorder(output_dir, channels=1, meeting_name="Quarterly planning " * 40)

        wav_path = recorder.start()
        recorder.stop()

        assert wav_path.exists()
        assert wav_path.suffix == ".wav"
        assert len(str(wav_path)) <= WINDOWS_SAFE_PATH_LIMIT
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_start_includes_hour_and_minute_and_uses_counter_within_same_minute():
    root = _make_test_root("test_recorder_same_minute_counter")
    try:
        fake_now = datetime(2026, 4, 14, 9, 30, 45)

        with patch("utils.filename_utils.datetime") as mock_datetime:
            mock_datetime.now.return_value = fake_now

            first = WavRecorder(root, channels=1, meeting_name="Weekly sync")
            first_path = first.start()
            first.stop()

            second = WavRecorder(root, channels=1, meeting_name="Weekly sync")
            second_path = second.start()
            second.stop()

        assert first_path.name == "2026-04-14_09_30_Weekly-sync.wav"
        assert second_path.name == "2026-04-14_09_30_Weekly-sync_2.wav"
    finally:
        shutil.rmtree(root, ignore_errors=True)
