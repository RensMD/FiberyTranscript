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


def test_get_session_snapshot_wraps_backend_snapshot():
    app = SimpleNamespace(
        get_session_snapshot=lambda: {
            "state": "prepared",
            "prepared_audio": {"file_path": "meeting.wav"},
            "has_linked_meeting": True,
            "entity_name": "Weekly sync",
            "entity_database": "Internal Meeting",
            "undo_available": True,
        }
    )
    bridge = ApiBridge(app)

    result = bridge.get_session_snapshot()

    assert result == {
        "success": True,
        "state": "prepared",
        "prepared_audio": {"file_path": "meeting.wav"},
        "has_linked_meeting": True,
        "entity_name": "Weekly sync",
        "entity_database": "Internal Meeting",
        "undo_available": True,
    }


def test_reset_session_keep_meeting_forwards_to_app():
    called = {"value": False}

    def _reset():
        called["value"] = True

    bridge = ApiBridge(SimpleNamespace(reset_session_keep_meeting=_reset))

    result = bridge.reset_session_keep_meeting()

    assert result == {"success": True}
    assert called["value"] is True


def test_stash_session_undo_snapshot_forwards_ttl():
    captured = {}

    def _stash(ttl_seconds):
        captured["ttl_seconds"] = ttl_seconds
        return {"stored": True, "undo_available": True, "ttl_seconds": ttl_seconds}

    bridge = ApiBridge(SimpleNamespace(stash_session_undo_snapshot=_stash))

    result = bridge.stash_session_undo_snapshot(12)

    assert result == {
        "success": True,
        "stored": True,
        "undo_available": True,
        "ttl_seconds": 12,
    }
    assert captured["ttl_seconds"] == 12


def test_undo_session_replace_returns_restored_snapshot():
    snapshot = {"state": "completed", "prepared_audio": {"file_path": "meeting.wav"}}
    bridge = ApiBridge(SimpleNamespace(undo_session_replace=lambda: snapshot))

    result = bridge.undo_session_replace()

    assert result == {"success": True, "snapshot": snapshot}


def test_undo_session_replace_returns_error_when_snapshot_expired():
    def _undo():
        raise RuntimeError("No replacement session is available to undo.")

    bridge = ApiBridge(SimpleNamespace(undo_session_replace=_undo))

    result = bridge.undo_session_replace()

    assert result == {
        "success": False,
        "error": "No replacement session is available to undo.",
    }
