import shutil
import time
import queue as pyqueue
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from audio import recorder as recorder_module
from audio.recorder import WavRecorder


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
