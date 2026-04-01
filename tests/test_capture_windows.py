import pytest

from audio.capture_windows import _LoopbackStallWatchdog


def test_watchdog_waits_for_grace_window_before_injecting_silence():
    watchdog = _LoopbackStallWatchdog(
        stall_timeout_seconds=0.2,
        emit_interval_seconds=0.1,
        start_time=0.0,
    )

    assert watchdog.poll_timeout(0.19) == (False, False)
    assert watchdog.stall_count == 0


def test_watchdog_injects_one_silence_chunk_per_cadence_during_stall():
    watchdog = _LoopbackStallWatchdog(
        stall_timeout_seconds=0.2,
        emit_interval_seconds=0.1,
        start_time=0.0,
    )

    assert watchdog.poll_timeout(0.21) == (True, True)
    assert watchdog.poll_timeout(0.25) == (False, False)
    assert watchdog.poll_timeout(0.31) == (False, True)
    assert watchdog.poll_timeout(0.35) == (False, False)


def test_watchdog_recovery_only_reports_once_per_stall():
    watchdog = _LoopbackStallWatchdog(
        stall_timeout_seconds=0.2,
        emit_interval_seconds=0.1,
        start_time=0.0,
    )

    watchdog.poll_timeout(0.21)
    watchdog.poll_timeout(0.31)

    recovery = watchdog.notify_data(0.36)
    assert recovery == pytest.approx(0.15)
    assert watchdog.stall_count == 1
    assert watchdog.longest_stall_seconds == pytest.approx(0.15)
    assert watchdog.notify_data(0.40) is None


def test_watchdog_finalize_tracks_active_stall_in_summary():
    watchdog = _LoopbackStallWatchdog(
        stall_timeout_seconds=0.2,
        emit_interval_seconds=0.1,
        start_time=0.0,
    )

    watchdog.poll_timeout(0.21)

    active = watchdog.finalize(0.50)
    assert active == pytest.approx(0.29)
    assert watchdog.longest_stall_seconds == pytest.approx(0.29)
