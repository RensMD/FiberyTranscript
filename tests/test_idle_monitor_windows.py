import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from audio.capture import AudioDevice
from config.settings import Settings


def _make_app():
    from app import FiberyTranscriptApp

    tmp = Path(tempfile.mkdtemp())
    settings = Settings(display_name="Test")
    return FiberyTranscriptApp(settings, tmp)


def _make_device(index: int, name: str, is_loopback: bool) -> AudioDevice:
    return AudioDevice(
        index=index,
        name=name,
        is_input=True,
        is_loopback=is_loopback,
        sample_rate=48000 if is_loopback else 44100,
        channels=2 if is_loopback else 1,
    )


def test_start_monitor_skips_loopback_capture_on_windows():
    app = _make_app()
    app.audio_capture = MagicMock()
    app.audio_capture.is_capturing.return_value = False

    mic = _make_device(1, "Mic", is_loopback=False)
    loop = _make_device(2, "Loop", is_loopback=True)
    app._find_device = MagicMock(side_effect=[mic, loop])

    with patch.object(sys, "platform", "win32"):
        app.start_monitor(1, 2)

    kwargs = app.audio_capture.start_capture.call_args.kwargs
    assert kwargs["mic_device"] == mic
    assert kwargs["loopback_device"] is None
    assert app._selected_mic_index == 1
    assert app._selected_sys_index == 2
    assert app._last_sys_level == 0.0


def test_start_monitor_keeps_loopback_capture_off_windows():
    app = _make_app()
    app.audio_capture = MagicMock()
    app.audio_capture.is_capturing.return_value = False

    mic = _make_device(1, "Mic", is_loopback=False)
    loop = _make_device(2, "Loop", is_loopback=True)
    app._find_device = MagicMock(side_effect=[mic, loop])

    with patch.object(sys, "platform", "linux"):
        app.start_monitor(1, 2)

    kwargs = app.audio_capture.start_capture.call_args.kwargs
    assert kwargs["mic_device"] == mic
    assert kwargs["loopback_device"] == loop


def test_start_monitor_can_include_loopback_preview_on_windows():
    app = _make_app()
    app.audio_capture = MagicMock()
    app.audio_capture.is_capturing.return_value = False

    mic = _make_device(1, "Mic", is_loopback=False)
    loop = _make_device(2, "Loop", is_loopback=True)
    app._find_device = MagicMock(side_effect=[mic, loop])

    with patch.object(sys, "platform", "win32"):
        app.start_monitor(1, 2, include_loopback=True)

    kwargs = app.audio_capture.start_capture.call_args.kwargs
    assert kwargs["mic_device"] == mic
    assert kwargs["loopback_device"] == loop


def test_start_monitor_is_noop_when_same_devices_are_already_monitored():
    app = _make_app()
    app.audio_capture = MagicMock()
    app.audio_capture.is_capturing.return_value = True
    app._selected_mic_index = 1
    app._selected_sys_index = 2

    app.start_monitor(1, 2)

    app.audio_capture.stop_capture.assert_not_called()
    app.audio_capture.start_capture.assert_not_called()


def test_start_monitor_restarts_when_loopback_preview_policy_changes():
    app = _make_app()
    app.audio_capture = MagicMock()
    app.audio_capture.is_capturing.return_value = True
    app._selected_mic_index = 1
    app._selected_sys_index = 2
    app._monitor_include_loopback = False

    mic = _make_device(1, "Mic", is_loopback=False)
    loop = _make_device(2, "Loop", is_loopback=True)
    app._find_device = MagicMock(side_effect=[mic, loop])

    with patch.object(sys, "platform", "win32"):
        app.start_monitor(1, 2, include_loopback=True)

    app.audio_capture.stop_capture.assert_called_once_with()
    kwargs = app.audio_capture.start_capture.call_args.kwargs
    assert kwargs["mic_device"] == mic
    assert kwargs["loopback_device"] == loop


def test_stop_monitor_resets_cached_levels_and_notifies_zeroes():
    app = _make_app()
    app.audio_capture = MagicMock()
    app.audio_capture.is_capturing.return_value = False
    app._notify_js = MagicMock()
    app._last_mic_level = 0.5
    app._last_raw_mic_level = 0.4
    app._last_sys_level = 0.7

    app.stop_monitor()

    assert app._last_mic_level == 0.0
    assert app._last_raw_mic_level == 0.0
    assert app._last_sys_level == 0.0
    app._notify_js.assert_called_once_with(
        "window.updateAudioLevels && window.updateAudioLevels(0.0, 0.0)"
    )
