"""Tests for AudioHealthMonitor: dead channel, clipping, speech, warnings."""

from unittest.mock import patch
from audio.health_monitor import (
    AudioHealth,
    AudioHealthMonitor,
    DEAD_CHANNEL_THRESHOLD,
    DEAD_CHANNEL_DURATION,
    CLIPPING_THRESHOLD,
    CLIPPING_DURATION,
    SPEECH_THRESHOLD,
)


class _FakeTime:
    """Deterministic monotonic clock for testing."""
    def __init__(self, start=100.0):
        self._now = start

    def monotonic(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


def _feed(monitor, mic_rms, sys_rms, seconds, clock, tick=0.1):
    """Feed ticks to the monitor, advancing the fake clock. Returns last non-None report."""
    result = None
    ticks = int(seconds / tick)
    for _ in range(ticks):
        clock.advance(tick)
        r = monitor.update(mic_rms, sys_rms)
        if r is not None:
            result = r
    return result


class TestAudioHealth:
    def test_defaults(self):
        h = AudioHealth()
        assert h.mic_alive is True
        assert h.sys_alive is True
        assert h.mic_clipping is False
        assert h.speech_detected is False
        assert h.silence_duration == 0.0

    def test_to_dict(self):
        h = AudioHealth(mic_alive=False, speech_detected=True)
        d = h.to_dict()
        assert d["mic_alive"] is False
        assert d["speech_detected"] is True


class TestDeadChannel:
    def test_mic_alive_with_signal(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.05, 0.05, 3.0, clock)
        assert result is not None
        assert result.mic_alive is True
        assert result.sys_alive is True

    def test_mic_dead_after_duration(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.0, 0.05, DEAD_CHANNEL_DURATION + 3.0, clock)
        assert result is not None
        assert result.mic_alive is False
        assert result.sys_alive is True

    def test_sys_dead_after_duration(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.05, 0.0, DEAD_CHANNEL_DURATION + 3.0, clock)
        assert result is not None
        assert result.sys_alive is False
        assert result.mic_alive is True

    def test_negative_rms_means_no_device(self):
        """mic_rms < 0 means no mic selected — should be treated as alive."""
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, -1.0, 0.05, 3.0, clock)
        assert result is not None
        assert result.mic_alive is True


class TestClipping:
    def test_no_clipping_under_threshold(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.5, 0.5, 3.0, clock)
        assert result is not None
        assert result.mic_clipping is False
        assert result.sys_clipping is False

    def test_clipping_above_threshold(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.98, 0.98, CLIPPING_DURATION + 3.0, clock)
        assert result is not None
        assert result.mic_clipping is True
        assert result.sys_clipping is True


class TestSpeechDetection:
    def test_speech_detected(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.1, 0.1, 3.0, clock)
        assert result is not None
        assert result.speech_detected is True

    def test_no_speech_when_silent(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.0, 0.0, 3.0, clock)
        assert result is not None
        assert result.speech_detected is False


class TestReportThrottling:
    def test_no_report_within_2s_of_previous(self):
        """After a report, the next update within 2s should return None."""
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            # First report comes after 2s
            _feed(m, 0.05, 0.05, 2.5, clock)
            # Next tick within 2s window should return None
            clock.advance(0.1)
            result = m.update(0.05, 0.05)
        assert result is None

    def test_returns_report_after_2s(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            result = _feed(m, 0.05, 0.05, 2.5, clock)
        assert result is not None


class TestReset:
    def test_reset_clears_state(self):
        clock = _FakeTime()
        with patch("audio.health_monitor.time") as mock_time:
            mock_time.monotonic = clock.monotonic
            m = AudioHealthMonitor()
            _feed(m, 0.0, 0.0, DEAD_CHANNEL_DURATION + 3.0, clock)
            m.reset()
            result = _feed(m, 0.05, 0.05, 3.0, clock)
        assert result is not None
        assert result.mic_alive is True
        assert result.sys_alive is True


class TestWarnings:
    def test_mic_dead_warning(self):
        m = AudioHealthMonitor()
        h = AudioHealth(mic_alive=False, sys_alive=True)
        warnings = m.check_warnings(h)
        assert len(warnings) == 1
        assert "Microphone" in warnings[0]

    def test_both_dead_warning(self):
        m = AudioHealthMonitor()
        h = AudioHealth(mic_alive=False, sys_alive=False)
        warnings = m.check_warnings(h)
        assert any("BOTH_DEAD:" in w for w in warnings)
        assert any("stopping" in w.lower() for w in warnings)

    def test_clipping_warning(self):
        m = AudioHealthMonitor()
        h = AudioHealth(mic_clipping=True)
        warnings = m.check_warnings(h)
        assert len(warnings) == 1
        assert "clipping" in warnings[0].lower()

    def test_dedup_warnings(self):
        """Same warning should not fire twice."""
        m = AudioHealthMonitor()
        h = AudioHealth(mic_alive=False, sys_alive=True)
        w1 = m.check_warnings(h)
        w2 = m.check_warnings(h)
        assert len(w1) == 1
        assert len(w2) == 0

    def test_dedup_resets_on_recovery(self):
        m = AudioHealthMonitor()
        h_dead = AudioHealth(mic_alive=False, sys_alive=True)
        m.check_warnings(h_dead)
        m.reset()
        w = m.check_warnings(h_dead)
        assert len(w) == 1
