import logging

import numpy as np
from unittest.mock import patch

from audio.mixer import AudioMixer, MIX_CHUNK_BYTES


class _FakeTime:
    def __init__(self, start: float = 0.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _make_chunk(value: int) -> bytes:
    samples = np.full(MIX_CHUNK_BYTES // 2, value, dtype=np.int16)
    return samples.tobytes()


def _decode_stereo(pcm_data: bytes) -> np.ndarray:
    return np.frombuffer(pcm_data, dtype=np.int16).reshape(-1, 2)


def test_stereo_continues_with_silent_loopback_when_loopback_stalls():
    clock = _FakeTime()
    mixed: list[bytes] = []

    with patch("audio.mixer.time") as mock_time:
        mock_time.monotonic = clock.monotonic
        mixer = AudioMixer(
            on_mixed_chunk=mixed.append,
            has_mic=True,
            has_loopback=True,
            stall_timeout_seconds=0.2,
        )
        mixer.add_mic_audio(_make_chunk(100))
        assert mixed == []

        clock.advance(0.25)
        mixer.add_mic_audio(_make_chunk(200))

    assert len(mixed) == 2
    first = _decode_stereo(mixed[0])
    second = _decode_stereo(mixed[1])
    assert np.all(first[:, 0] == 100)
    assert np.all(first[:, 1] == 0)
    assert np.all(second[:, 0] == 200)
    assert np.all(second[:, 1] == 0)


def test_initial_missing_loopback_does_not_log_a_stall_warning(caplog):
    clock = _FakeTime()
    mixed: list[bytes] = []

    with patch("audio.mixer.time") as mock_time:
        mock_time.monotonic = clock.monotonic
        with caplog.at_level(logging.WARNING):
            mixer = AudioMixer(
                on_mixed_chunk=mixed.append,
                has_mic=True,
                has_loopback=True,
                stall_timeout_seconds=0.2,
            )
            mixer.add_mic_audio(_make_chunk(100))
            clock.advance(0.25)
            mixer.add_mic_audio(_make_chunk(200))

    assert len(mixed) == 2
    assert "loopback source stalled" not in caplog.text


def test_stereo_continues_with_silent_mic_when_mic_stalls():
    clock = _FakeTime()
    mixed: list[bytes] = []

    with patch("audio.mixer.time") as mock_time:
        mock_time.monotonic = clock.monotonic
        mixer = AudioMixer(
            on_mixed_chunk=mixed.append,
            has_mic=True,
            has_loopback=True,
            stall_timeout_seconds=0.2,
        )
        mixer.add_loopback_audio(_make_chunk(300))
        assert mixed == []

        clock.advance(0.25)
        mixer.add_loopback_audio(_make_chunk(400))

    assert len(mixed) == 2
    first = _decode_stereo(mixed[0])
    second = _decode_stereo(mixed[1])
    assert np.all(first[:, 0] == 0)
    assert np.all(first[:, 1] == 300)
    assert np.all(second[:, 0] == 0)
    assert np.all(second[:, 1] == 400)


def test_loopback_stall_warning_still_fires_after_audio_has_started(caplog):
    clock = _FakeTime()
    mixed: list[bytes] = []

    with patch("audio.mixer.time") as mock_time:
        mock_time.monotonic = clock.monotonic
        mixer = AudioMixer(
            on_mixed_chunk=mixed.append,
            has_mic=True,
            has_loopback=True,
            stall_timeout_seconds=0.2,
        )
        mixer.add_mic_audio(_make_chunk(10))
        mixer.add_loopback_audio(_make_chunk(90))
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            clock.advance(0.25)
            mixer.add_mic_audio(_make_chunk(20))

    assert "loopback source stalled" in caplog.text


def test_stall_recovery_stops_silence_padding():
    clock = _FakeTime()
    mixed: list[bytes] = []

    with patch("audio.mixer.time") as mock_time:
        mock_time.monotonic = clock.monotonic
        mixer = AudioMixer(
            on_mixed_chunk=mixed.append,
            has_mic=True,
            has_loopback=True,
            stall_timeout_seconds=0.2,
        )
        mixer.add_mic_audio(_make_chunk(10))
        clock.advance(0.25)
        mixer.add_mic_audio(_make_chunk(20))
        clock.advance(0.01)
        mixer.add_loopback_audio(_make_chunk(99))
        mixer.add_mic_audio(_make_chunk(30))

    assert len(mixed) == 3
    recovered = _decode_stereo(mixed[2])
    assert np.all(recovered[:, 0] == 30)
    assert np.all(recovered[:, 1] == 99)


def test_mono_path_still_passthroughs_active_source():
    mixed: list[bytes] = []
    mixer = AudioMixer(on_mixed_chunk=mixed.append, has_mic=True, has_loopback=False)

    source = _make_chunk(55)
    mixer.add_mic_audio(source)

    assert mixed == [source]


def test_explicit_stereo_output_keeps_silent_channel_for_single_source():
    mixed: list[bytes] = []
    mixer = AudioMixer(
        on_mixed_chunk=mixed.append,
        has_mic=True,
        has_loopback=False,
        output_channels=2,
    )

    mixer.add_mic_audio(_make_chunk(42))

    assert len(mixed) == 1
    stereo = _decode_stereo(mixed[0])
    assert np.all(stereo[:, 0] == 42)
    assert np.all(stereo[:, 1] == 0)


def test_explicit_mono_output_mixes_both_sources():
    mixed: list[bytes] = []
    mixer = AudioMixer(
        on_mixed_chunk=mixed.append,
        has_mic=True,
        has_loopback=True,
        output_channels=1,
    )

    mixer.add_mic_audio(_make_chunk(100))
    mixer.add_loopback_audio(_make_chunk(25))

    assert len(mixed) == 1
    mono = np.frombuffer(mixed[0], dtype=np.int16)
    assert np.all(mono == 125)
