from types import SimpleNamespace

from ui.api_bridge import ApiBridge


def test_get_file_dialog_type_prefers_modern_pywebview_enum():
    webview_module = SimpleNamespace(
        FileDialog=SimpleNamespace(OPEN="modern-open", FOLDER="modern-folder"),
        OPEN_DIALOG="legacy-open",
        FOLDER_DIALOG="legacy-folder",
    )

    assert ApiBridge._get_file_dialog_type(webview_module, "open") == "modern-open"
    assert ApiBridge._get_file_dialog_type(webview_module, "folder") == "modern-folder"


def test_get_file_dialog_type_falls_back_to_legacy_constants():
    webview_module = SimpleNamespace(
        OPEN_DIALOG="legacy-open",
        FOLDER_DIALOG="legacy-folder",
    )

    assert ApiBridge._get_file_dialog_type(webview_module, "open") == "legacy-open"
    assert ApiBridge._get_file_dialog_type(webview_module, "folder") == "legacy-folder"


def test_start_transcription_forwards_recording_mode():
    app = SimpleNamespace()
    captured = {}

    def _start_transcription(options):
        captured["options"] = options
        return {"success": True}

    app.start_transcription = _start_transcription
    bridge = ApiBridge(app)

    result = bridge.start_transcription(
        False,
        True,
        "replace",
        "mic_and_speakers",
    )

    assert result == {"success": True}
    assert captured["options"].transcript_mode == "replace"
    assert captured["options"].recording_mode == "mic_and_speakers"


def test_generate_summary_forwards_summary_language(monkeypatch):
    notifications = []
    app = SimpleNamespace()
    app._notify_js = notifications.append
    app.generate_summary = lambda **kwargs: {"success": True, "summary": kwargs["summary_language"]}
    bridge = ApiBridge(app)

    class _ImmediateThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr("ui.api_bridge.threading.Thread", _ImmediateThread)

    result = bridge.generate_summary(["summarize"], "prompt", "short", "nl")

    assert result["success"] is True
    assert notifications == ['window.onSummarizeComplete({"success": true, "summary": "nl"})']
