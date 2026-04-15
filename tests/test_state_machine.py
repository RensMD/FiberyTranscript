"""Tests for FiberyTranscriptApp state machine: transitions, guards, close confirmation."""

import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import shutil
import tempfile
import threading

import pytest

from integrations.fibery_client import EntityContext
from config.settings import Settings
from config.session import RecordingSession, SessionContext, SessionResults
from transcription.formatter import format_diarized_transcript
from utils.filename_utils import WINDOWS_SAFE_PATH_LIMIT


def _make_app(settings: Settings | None = None, data_dir: Path | None = None):
    """Create a minimal FiberyTranscriptApp for state testing."""
    from app import FiberyTranscriptApp
    tmp = data_dir or Path(tempfile.mkdtemp())
    settings = settings or Settings(display_name="Test")
    app = FiberyTranscriptApp(settings, tmp)
    return app


def _make_test_root(name: str) -> Path:
    root = Path.cwd() / "data" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_pending_session(*, wav_path: str = "meeting.wav") -> RecordingSession:
    session = RecordingSession(SessionContext(wav_path=wav_path))
    session.results.set_batch_result({
        "utterances": [{"speaker": "A", "text": "Pending transcript", "start": 0, "end": 1}]
    })
    session.results.set_cleaned_transcript("Pending transcript")
    session.results.set_generated_summary("Pending summary")
    return session


def _thread_spy(spawned: list, *, auto_run: bool = False):
    class _ThreadSpy:
        def __init__(self, *, target, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self.daemon = daemon
            self.started = False
            spawned.append(self)

        def start(self):
            self.started = True
            if auto_run:
                self._target(*self._args, **self._kwargs)

    return _ThreadSpy


class _FakeAudioSegment:
    def __init__(self, duration_ms: int, frame_rate: int, channels: int):
        self._duration_ms = duration_ms
        self.frame_rate = frame_rate
        self.channels = channels

    def __len__(self):
        return self._duration_ms


def _prepared_audio_info_for_path(path: Path, is_uploaded_file: bool = False) -> dict:
    return {
        "file_path": str(path),
        "file_name": path.name,
        "is_uploaded_file": is_uploaded_file,
        "recording_mode_recommendation": "mic_only",
        "recording_mode_reason": "",
    }


class TestStateConstants:
    def test_state_values(self):
        from app import FiberyTranscriptApp
        assert FiberyTranscriptApp.STATE_IDLE == "idle"
        assert FiberyTranscriptApp.STATE_RECORDING == "recording"
        assert FiberyTranscriptApp.STATE_PREPARED == "prepared"
        assert FiberyTranscriptApp.STATE_PROCESSING == "processing"
        assert FiberyTranscriptApp.STATE_COMPLETED == "completed"

    def test_initial_state(self):
        app = _make_app()
        assert app.state == "idle"


class TestNeedsCloseConfirmation:
    def test_idle_no_confirm(self):
        app = _make_app()
        app.state = "idle"
        assert app.needs_close_confirmation is False

    def test_recording_needs_confirm(self):
        app = _make_app()
        app.state = "recording"
        assert app.needs_close_confirmation is True

    def test_processing_needs_confirm(self):
        app = _make_app()
        app.state = "processing"
        assert app.needs_close_confirmation is True

    def test_completed_no_entity_not_copied(self):
        """Completed with transcript but not copied → needs confirmation."""
        app = _make_app()
        app.state = "completed"
        ctx = SessionContext(entity=None)
        session = RecordingSession(ctx)
        session.results.set_batch_result({"utterances": []})
        app._session = session
        assert app.needs_close_confirmation is True

    def test_completed_no_entity_copied(self):
        """Completed with transcript and copied → no confirmation needed."""
        app = _make_app()
        app.state = "completed"
        ctx = SessionContext(entity=None)
        session = RecordingSession(ctx)
        session.results.set_batch_result({"utterances": []})
        session.results.set_user_has_copied()
        app._session = session
        assert app.needs_close_confirmation is False

    def test_completed_entity_transcript_not_sent(self):
        """Entity linked but transcript not sent → needs confirmation."""
        app = _make_app()
        app.state = "completed"
        ctx = SessionContext(entity="some-entity")
        session = RecordingSession(ctx)
        app._session = session
        assert app.needs_close_confirmation is True

    def test_completed_entity_transcript_sent(self):
        """Entity linked and transcript sent, no summary → no confirmation."""
        app = _make_app()
        app.state = "completed"
        ctx = SessionContext(entity="some-entity")
        session = RecordingSession(ctx)
        session.results.try_start_transcript_send()
        session.results.finish_transcript_send(success=True)
        app._session = session
        assert app.needs_close_confirmation is False

    def test_completed_entity_transcript_sent_summary_unsent(self):
        """Entity linked, transcript sent, summary generated but NOT sent → needs confirmation."""
        app = _make_app()
        app.state = "completed"
        ctx = SessionContext(entity="some-entity")
        session = RecordingSession(ctx)
        session.results.try_start_transcript_send()
        session.results.finish_transcript_send(success=True)
        session.results.set_generated_summary("Some summary")
        app._session = session
        assert app.needs_close_confirmation is True

    def test_completed_entity_transcript_and_summary_sent(self):
        """Entity linked, both transcript and summary sent → no confirmation."""
        app = _make_app()
        app.state = "completed"
        ctx = SessionContext(entity="some-entity")
        session = RecordingSession(ctx)
        session.results.try_start_transcript_send()
        session.results.finish_transcript_send(success=True)
        session.results.set_generated_summary("Some summary")
        session.results.try_start_summary_send()
        session.results.finish_summary_send(success=True)
        app._session = session
        assert app.needs_close_confirmation is False

    def test_completed_no_session(self):
        """Completed with no session object → no confirmation."""
        app = _make_app()
        app.state = "completed"
        app._session = None
        assert app.needs_close_confirmation is False


class TestResetSession:
    def test_resets_to_idle(self):
        app = _make_app()
        app.state = "completed"
        ctx = SessionContext(entity="test")
        app._session = RecordingSession(ctx)
        app._validated_entity = "something"
        app._entity_context = "context"
        app._linked_transcript_text = "Existing transcript"
        app._recording_segments = [Path("seg1.wav")]
        app._sleeping = True
        app._transcript_mode = "replace"
        app._recording_mode = "mic_and_speakers"
        app._summary_mode = "replace"
        app._summary_language = "nl"

        app.reset_session()

        assert app.state == "idle"
        assert app._session is None
        assert app._validated_entity is None
        assert app._entity_context is None
        assert app._linked_transcript_text == ""
        assert app._recording_segments == []
        assert app._sleeping is False
        assert app._transcript_mode == "append"
        assert app._recording_mode == "mic_only"
        assert app._summary_mode == "append"
        assert app._summary_language == "en"


class TestSessionSnapshots:
    @pytest.mark.parametrize("state", ["idle", "recording", "prepared", "processing", "completed"])
    def test_get_session_snapshot_reports_state_and_linked_meeting_metadata(self, state):
        app = _make_app()
        entity = SimpleNamespace(entity_name="Weekly sync", database="Internal Meeting")
        app.state = state

        if state == app.STATE_RECORDING:
            app._session = RecordingSession(SessionContext(entity=entity, wav_path="meeting.wav"))
        else:
            app._validated_entity = entity

        if state in (app.STATE_PREPARED, app.STATE_COMPLETED):
            app._session = RecordingSession(SessionContext(entity=entity, wav_path="meeting.wav"))
            app._prepared_audio_info = {"file_path": "meeting.wav", "file_name": "meeting.wav"}

        snapshot = app.get_session_snapshot()

        assert snapshot["state"] == state
        assert snapshot["has_linked_meeting"] is True
        assert snapshot["entity_name"] == "Weekly sync"
        assert snapshot["entity_database"] == "Internal Meeting"
        assert snapshot["undo_available"] is False
        if state in (app.STATE_PREPARED, app.STATE_COMPLETED):
            assert snapshot["prepared_audio"]["file_path"] == "meeting.wav"
        else:
            assert snapshot["prepared_audio"] is None

    def test_get_session_snapshot_prunes_expired_undo_snapshot(self, monkeypatch):
        app = _make_app()
        app._undo_snapshot = {"state": app.STATE_COMPLETED}
        app._undo_snapshot_expires_at = 10.0

        monkeypatch.setattr("app.time.monotonic", lambda: 11.0)

        snapshot = app.get_session_snapshot()

        assert snapshot["undo_available"] is False
        assert app._undo_snapshot is None


class TestResetSessionKeepMeeting:
    def test_preserves_meeting_context_and_modes_while_clearing_workflow_outputs(self):
        app = _make_app()
        entity = SimpleNamespace(entity_name="Weekly sync", database="Internal Meeting")
        app.state = app.STATE_COMPLETED
        app._validated_entity = entity
        app._entity_context = {"company": "Fibery"}
        app._linked_transcript_text = "Existing Fibery transcript"
        app._transcript_mode = "replace"
        app._recording_mode = "mic_and_speakers"
        app._summary_mode = "replace"
        app._summary_language = "nl"
        app._prepared_audio_info = {"file_path": "meeting.wav"}
        app._session = RecordingSession(SessionContext(entity=entity, wav_path="meeting.wav"))
        app._resume_background_scanning = MagicMock()
        start_token = app._session_token

        app.reset_session_keep_meeting()

        assert app.state == app.STATE_IDLE
        assert app._validated_entity is entity
        assert app._entity_context == {"company": "Fibery"}
        assert app._linked_transcript_text == "Existing Fibery transcript"
        assert app._transcript_mode == "replace"
        assert app._recording_mode == "mic_and_speakers"
        assert app._summary_mode == "replace"
        assert app._summary_language == "nl"
        assert app._prepared_audio_info is None
        assert app._session is None
        assert app._session_token == start_token + 1
        app._resume_background_scanning.assert_called_once_with()


class TestMeetingLinkState:
    def test_deselect_meeting_clears_linked_transcript(self):
        app = _make_app()
        app._validated_entity = "entity"
        app._linked_transcript_text = "Existing transcript"
        app.release_recording_lock = MagicMock()

        app.deselect_meeting()

        assert app._validated_entity is None
        assert app._linked_transcript_text == ""


class TestLinkedTranscriptSummary:
    def test_generate_summary_uses_linked_transcript_without_batch_result(self):
        app = _make_app()
        app._linked_transcript_text = "Existing Fibery transcript"
        app._validated_entity = SimpleNamespace(space="General", database="Internal Meeting")
        app._fibery_client = MagicMock()
        app._fibery_client.get_entity_notes.return_value = "Notes"
        app._fetch_entity_context = MagicMock(return_value=None)

        with patch("app.get_key", return_value="gemini-key"):
            with patch("integrations.gemini_client.summarize_transcript", return_value="Summary text") as summarize:
                result = app.generate_summary()

        assert result["success"] is True
        assert result["summary"] == "Summary text"
        assert result["sent_to_fibery"] is True
        assert summarize.call_args.kwargs["transcript"] == "Existing Fibery transcript"
        app._fibery_client.update_summary_only.assert_called_once_with(
            app._validated_entity,
            ai_summary="Summary text",
            append=True,
        )

    def test_generate_summary_prefers_local_transcript_over_linked_transcript(self):
        app = _make_app()
        app._linked_transcript_text = "Linked transcript"
        app._session = RecordingSession(SessionContext())
        app._session.results.set_batch_result({
            "utterances": [{"speaker": "A", "text": "Raw transcript", "start": 0, "end": 1}]
        })
        app._session.results.set_cleaned_transcript("Local cleaned transcript")

        with patch("app.get_key", return_value="gemini-key"):
            with patch("integrations.gemini_client.summarize_transcript", return_value="Summary text") as summarize:
                result = app.generate_summary()

        assert result["success"] is True
        assert result["sent_to_fibery"] is False
        assert summarize.call_args.kwargs["transcript"] == "Local cleaned transcript"

    def test_generate_summary_uses_summary_mode_for_fibery_write(self):
        app = _make_app()
        app._summary_mode = "replace"
        app._linked_transcript_text = "Existing Fibery transcript"
        app._validated_entity = SimpleNamespace(space="General", database="Internal Meeting")
        app._fibery_client = MagicMock()
        app._fibery_client.get_entity_notes.return_value = "Notes"
        app._fetch_entity_context = MagicMock(return_value=None)

        with patch("app.get_key", return_value="gemini-key"):
            with patch("integrations.gemini_client.summarize_transcript", return_value="Summary text"):
                result = app.generate_summary()

        assert result["success"] is True
        app._fibery_client.update_summary_only.assert_called_once_with(
            app._validated_entity,
            ai_summary="Summary text",
            append=False,
        )

    def test_generate_summary_remembers_summary_language_within_session(self):
        app = _make_app()
        app._linked_transcript_text = "Existing Fibery transcript"

        with patch("app.get_key", return_value="gemini-key"):
            with patch("integrations.gemini_client.summarize_transcript", return_value="Summary text") as summarize:
                result = app.generate_summary(summary_language="nl")

        assert result["success"] is True
        assert summarize.call_args.kwargs["summary_language"] == "nl"
        assert app._summary_language == "nl"

    def test_send_pending_summary_uses_summary_mode(self):
        app = _make_app()
        app._summary_mode = "replace"
        app._session = RecordingSession(SessionContext())
        app._session.results.set_generated_summary("Pending summary")
        app._validated_entity = SimpleNamespace(space="General", database="Internal Meeting")
        app._fibery_client = MagicMock()

        result = app.send_pending_summary_to_fibery()

        assert result["success"] is True
        app._fibery_client.update_summary_only.assert_called_once_with(
            app._validated_entity,
            ai_summary="Pending summary",
            append=False,
        )


class TestMeetingMetadata:
    def test_validate_fibery_url_returns_transcript_metadata(self):
        app = _make_app()
        entity = SimpleNamespace(
            space="General",
            database="Internal Meeting",
            entity_name="Weekly sync",
            internal_id="123",
            uuid="entity-uuid",
        )
        client = MagicMock()
        client.extract_url_candidates.return_value = ["https://example.fibery.io/General/Internal_Meeting/weekly-sync-123"]
        client.parse_url.return_value = entity
        client.get_entity_uuid.return_value = "entity-uuid"
        client.get_entity_name.return_value = "Weekly sync"
        client.get_entity_transcript.return_value = "Existing Fibery transcript"

        with patch("app.get_key", return_value="fibery-key"):
            with patch("integrations.fibery_client.FiberyClient", return_value=client):
                result = app.validate_fibery_url("https://example.fibery.io/General/Internal_Meeting/weekly-sync-123")

        assert result["success"] is True
        assert result["has_transcript"] is True
        assert result["transcript_text"] == "Existing Fibery transcript"
        assert app._linked_transcript_text == "Existing Fibery transcript"

    def test_create_fibery_meeting_returns_empty_transcript_metadata(self):
        app = _make_app()
        entity = SimpleNamespace(
            space="General",
            database="Internal Meeting",
            entity_name="New meeting",
            internal_id="456",
            uuid="entity-uuid",
        )
        client = MagicMock()
        client.create_entity.return_value = entity
        client.get_entity_url.return_value = "https://example.fibery.io/General/Internal_Meeting/new-meeting-456"

        with patch("app.get_key", return_value="fibery-key"):
            with patch("integrations.fibery_client.FiberyClient", return_value=client):
                result = app.create_fibery_meeting("internal", "New meeting")

        assert result["success"] is True
        assert result["has_transcript"] is False
        assert result["transcript_text"] == ""
        assert app._linked_transcript_text == ""

    @pytest.mark.parametrize("meeting_type", ["internal", "external"])
    def test_create_fibery_meeting_requires_name_when_blank(self, meeting_type):
        app = _make_app()
        client = MagicMock()

        with patch("app.get_key", return_value="fibery-key"):
            with patch("integrations.fibery_client.FiberyClient", return_value=client):
                result = app.create_fibery_meeting(meeting_type, "")

        assert result == {"success": False, "error": "Meeting name is required"}
        client.create_entity.assert_not_called()

    def test_create_fibery_meeting_allows_blank_interview_name_with_placeholder(self):
        app = _make_app()
        entity = SimpleNamespace(
            entity_name="Autocomposed interview name",
            database="Market Interview",
            space="Market",
            internal_id="456",
            uuid="uuid-123",
        )
        client = MagicMock()
        client.create_entity.return_value = entity
        client.get_entity_url.return_value = "https://example.fibery.io/Market/Market_Interview/autocomposed-interview-name-456"

        with patch("app.get_key", return_value="fibery-key"):
            with patch("integrations.fibery_client.FiberyClient", return_value=client):
                result = app.create_fibery_meeting("interview", "   ")

        assert result["success"] is True
        assert result["entity_name"] == "Autocomposed interview name"
        client.create_entity.assert_called_once()
        create_call = client.create_entity.call_args.kwargs
        assert create_call["space"] == "Market"
        assert create_call["database"] == "Market/Market Interview"
        assert create_call["name"].startswith("Interview placeholder ")

    def test_check_problems_ready_reports_existing_problem_sources(self):
        app = _make_app()
        app._validated_entity = SimpleNamespace(space="Market", database="Market Interview")
        app._fibery_client = MagicMock()
        app._fibery_client.get_entity_transcript.return_value = "Interview transcript"
        app._fibery_client.get_entity_notes.return_value = ""

        result = app.check_problems_ready()

        assert result == {
            "success": True,
            "has_notes": False,
            "has_transcript": True,
        }

    def test_check_problems_ready_surfaces_fetch_errors_when_no_source_is_confirmed(self):
        app = _make_app()
        app._validated_entity = SimpleNamespace(space="Market", database="Market Interview")
        app._fibery_client = MagicMock()
        app._fibery_client.get_entity_transcript.side_effect = RuntimeError("401 Unauthorized")
        app._fibery_client.get_entity_notes.return_value = ""

        result = app.check_problems_ready()

        assert result["success"] is False
        assert "Could not fetch transcript from Fibery" in result["error"]
        assert "Authentication failed. Check your API keys in Settings." in result["error"]

    def test_check_problems_ready_keeps_success_when_other_source_has_content(self):
        app = _make_app()
        app._validated_entity = SimpleNamespace(space="Market", database="Market Interview")
        app._fibery_client = MagicMock()
        app._fibery_client.get_entity_transcript.side_effect = RuntimeError("timeout")
        app._fibery_client.get_entity_notes.return_value = "Interview notes"

        result = app.check_problems_ready()

        assert result == {
            "success": True,
            "has_notes": True,
            "has_transcript": False,
        }

    def test_validate_fibery_url_retargets_pending_auto_flush_within_same_session(self, monkeypatch):
        app = _make_app(Settings(display_name="Test", audio_storage="fibery"))
        app._session = _make_pending_session()
        spawned = []
        entity_a = SimpleNamespace(space="General", database="Internal Meeting", entity_name="A", uuid="entity-a")
        entity_b = SimpleNamespace(space="General", database="Internal Meeting", entity_name="B", uuid="entity-b")
        client = MagicMock()
        client.extract_url_candidates.side_effect = [["https://example.fibery.io/a"], ["https://example.fibery.io/b"]]
        client.parse_url.side_effect = [entity_a, entity_b]
        client.get_entity_uuid.side_effect = ["entity-a", "entity-b"]
        client.get_entity_name.side_effect = ["Meeting A", "Meeting B"]
        client.get_entity_transcript.return_value = ""

        monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

        with patch("app.get_key", return_value="fibery-key"):
            with patch("integrations.fibery_client.FiberyClient", return_value=client):
                assert app.validate_fibery_url("https://example.fibery.io/a")["success"] is True
                assert app.validate_fibery_url("https://example.fibery.io/b")["success"] is True

        assert len(spawned) == 4
        transcript_thread, summary_thread = spawned[-2:]
        assert transcript_thread._target == app._auto_send_transcript
        assert transcript_thread._args == (entity_b, client, app._session, app._session_token)
        assert summary_thread._target == app._auto_send_pending_summary
        assert summary_thread._args == (entity_b, client, app._session, app._session_token)

    def test_validate_fibery_url_does_not_auto_flush_after_reset_session(self, monkeypatch):
        app = _make_app(Settings(display_name="Test", audio_storage="fibery"))
        app._session = _make_pending_session()
        app.release_recording_lock = MagicMock()
        app.reset_session()

        spawned = []
        entity = SimpleNamespace(space="General", database="Internal Meeting", entity_name="B", uuid="entity-b")
        client = MagicMock()
        client.extract_url_candidates.return_value = ["https://example.fibery.io/b"]
        client.parse_url.return_value = entity
        client.get_entity_uuid.return_value = "entity-b"
        client.get_entity_name.return_value = "Meeting B"
        client.get_entity_transcript.return_value = ""

        monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

        with patch("app.get_key", return_value="fibery-key"):
            with patch("integrations.fibery_client.FiberyClient", return_value=client):
                result = app.validate_fibery_url("https://example.fibery.io/b")

        assert result["success"] is True
        assert spawned == []

    def test_start_transcription_freezes_selected_meeting_at_click_time(self, monkeypatch):
        app = _make_app(Settings(display_name="Test", audio_storage="fibery"))
        entity_at_prepare = SimpleNamespace(space="General", database="Internal Meeting", entity_name="A", uuid="entity-a")
        entity_at_click = SimpleNamespace(space="General", database="Internal Meeting", entity_name="B", uuid="entity-b")
        client_at_prepare = MagicMock()
        client_at_click = MagicMock()
        app._session = RecordingSession(SessionContext(
            entity=entity_at_prepare,
            fibery_client=client_at_prepare,
            wav_path="meeting.wav",
            compressed_path="meeting.ogg",
        ))
        app._prepared_audio_info = {"file_path": "meeting.wav"}
        app._validated_entity = entity_at_click
        app._fibery_client = client_at_click
        app._entity_context = {"company": "Fibery"}
        app.state = app.STATE_PREPARED

        spawned = []
        app._run_batch_processing = MagicMock()
        monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

        result = app.start_transcription()

        assert result["success"] is True
        assert len(spawned) == 1
        batch_thread = spawned[0]
        assert batch_thread._target == app._run_batch_processing
        session_arg = batch_thread._args[0]
        assert session_arg.context.entity is entity_at_click
        assert session_arg.context.fibery_client is client_at_click
        assert session_arg.context.entity_context == {"company": "Fibery"}
        assert session_arg.context.wav_path == "meeting.wav"

    def test_start_transcription_renames_placeholder_recording_for_selected_entity(self, monkeypatch):
        root = _make_test_root("test_start_transcription_renames_placeholder")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            entity = SimpleNamespace(space="General", database="Internal Meeting", entity_name="Weekly sync", uuid="entity-b")
            recordings_dir = app.data_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            wav_path = recordings_dir / "20260414_0930_recording.wav"
            ogg_path = recordings_dir / "20260414_0930_recording.ogg"
            wav_path.write_bytes(b"wav")
            ogg_path.write_bytes(b"ogg")
            app._session = RecordingSession(SessionContext(
                wav_path=str(wav_path),
                compressed_path=str(ogg_path),
                is_uploaded_file=False,
            ))
            app._prepared_audio_info = _prepared_audio_info_for_path(wav_path)
            app._validated_entity = entity
            app._fibery_client = MagicMock()
            app.state = app.STATE_PREPARED
            app._run_batch_processing = MagicMock()
            app._build_prepared_audio_info = MagicMock(
                side_effect=lambda path, is_uploaded: _prepared_audio_info_for_path(path, is_uploaded)
            )

            spawned = []
            monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

            result = app.start_transcription()

            renamed_wav = recordings_dir / "20260414_0930_Weekly-sync.wav"
            renamed_ogg = recordings_dir / "20260414_0930_Weekly-sync.ogg"
            assert result["success"] is True
            assert not wav_path.exists()
            assert not ogg_path.exists()
            assert renamed_wav.exists()
            assert renamed_ogg.exists()
            assert app._prepared_audio_info["file_name"] == renamed_wav.name
            assert result["prepared_audio"]["file_name"] == renamed_wav.name
            assert app._session.context.wav_path == str(renamed_wav)
            assert app._session.context.compressed_path == str(renamed_ogg)
            session_arg = spawned[0]._args[0]
            assert session_arg.context.entity is entity
            assert session_arg.context.wav_path == str(renamed_wav)
            assert session_arg.context.compressed_path == str(renamed_ogg)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_start_transcription_preserves_merged_prefix_when_renaming_placeholder_recording(self, monkeypatch):
        root = _make_test_root("test_start_transcription_renames_merged_placeholder")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            recordings_dir = app.data_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            wav_path = recordings_dir / "merged_20260414_0930_recording.wav"
            wav_path.write_bytes(b"wav")
            app._session = RecordingSession(SessionContext(
                wav_path=str(wav_path),
                compressed_path="",
                is_uploaded_file=False,
            ))
            app._prepared_audio_info = _prepared_audio_info_for_path(wav_path)
            app._validated_entity = SimpleNamespace(entity_name="Market interview")
            app.state = app.STATE_PREPARED
            app._run_batch_processing = MagicMock()
            app._build_prepared_audio_info = MagicMock(
                side_effect=lambda path, is_uploaded: _prepared_audio_info_for_path(path, is_uploaded)
            )

            spawned = []
            monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

            result = app.start_transcription()

            renamed_wav = recordings_dir / "merged_20260414_0930_Market-interview.wav"
            assert result["success"] is True
            assert not wav_path.exists()
            assert renamed_wav.exists()
            assert app._session.context.wav_path == str(renamed_wav)
            assert spawned[0]._args[0].context.wav_path == str(renamed_wav)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_start_transcription_renames_placeholder_recording_with_shared_collision_suffix(self, monkeypatch):
        root = _make_test_root("test_start_transcription_rename_collision")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            recordings_dir = app.data_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            wav_path = recordings_dir / "20260414_0930_recording.wav"
            ogg_path = recordings_dir / "20260414_0930_recording.ogg"
            wav_path.write_bytes(b"wav")
            ogg_path.write_bytes(b"ogg")
            (recordings_dir / "20260414_0930_Weekly-sync.wav").write_bytes(b"other-wav")
            (recordings_dir / "20260414_0930_Weekly-sync.ogg").write_bytes(b"other-ogg")
            app._session = RecordingSession(SessionContext(
                wav_path=str(wav_path),
                compressed_path=str(ogg_path),
                is_uploaded_file=False,
            ))
            app._prepared_audio_info = _prepared_audio_info_for_path(wav_path)
            app._validated_entity = SimpleNamespace(entity_name="Weekly sync")
            app.state = app.STATE_PREPARED
            app._run_batch_processing = MagicMock()
            app._build_prepared_audio_info = MagicMock(
                side_effect=lambda path, is_uploaded: _prepared_audio_info_for_path(path, is_uploaded)
            )

            spawned = []
            monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

            result = app.start_transcription()

            renamed_wav = recordings_dir / "20260414_0930_Weekly-sync_2.wav"
            renamed_ogg = recordings_dir / "20260414_0930_Weekly-sync_2.ogg"
            assert result["success"] is True
            assert renamed_wav.exists()
            assert renamed_ogg.exists()
            assert app._session.context.wav_path == str(renamed_wav)
            assert app._session.context.compressed_path == str(renamed_ogg)
            assert spawned[0]._args[0].context.wav_path == str(renamed_wav)
            assert spawned[0]._args[0].context.compressed_path == str(renamed_ogg)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_start_transcription_ignores_orphaned_target_ogg_when_recording_has_no_ogg_source(self, monkeypatch):
        root = _make_test_root("test_start_transcription_rename_orphan_ogg")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            recordings_dir = app.data_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            wav_path = recordings_dir / "20260414_0930_recording.wav"
            wav_path.write_bytes(b"wav")
            orphan_ogg = recordings_dir / "20260414_0930_Weekly-sync.ogg"
            orphan_ogg.write_bytes(b"orphan-ogg")
            app._session = RecordingSession(SessionContext(
                wav_path=str(wav_path),
                compressed_path="",
                is_uploaded_file=False,
            ))
            app._prepared_audio_info = _prepared_audio_info_for_path(wav_path)
            app._validated_entity = SimpleNamespace(entity_name="Weekly sync")
            app.state = app.STATE_PREPARED
            app._run_batch_processing = MagicMock()
            app._build_prepared_audio_info = MagicMock(
                side_effect=lambda path, is_uploaded: _prepared_audio_info_for_path(path, is_uploaded)
            )

            spawned = []
            monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

            result = app.start_transcription()

            renamed_wav = recordings_dir / "20260414_0930_Weekly-sync.wav"
            assert result["success"] is True
            assert renamed_wav.exists()
            assert orphan_ogg.exists()
            assert app._session.context.wav_path == str(renamed_wav)
            assert app._session.context.compressed_path == ""
            assert spawned[0]._args[0].context.wav_path == str(renamed_wav)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_start_transcription_does_not_rename_uploaded_audio_even_with_placeholder_stem(self, monkeypatch):
        root = _make_test_root("test_start_transcription_uploaded_no_rename")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            wav_path = root / "20260414_0930_recording.wav"
            wav_path.write_bytes(b"wav")
            app._session = RecordingSession(SessionContext(
                wav_path=str(wav_path),
                compressed_path="",
                is_uploaded_file=True,
            ))
            app._prepared_audio_info = _prepared_audio_info_for_path(wav_path, is_uploaded_file=True)
            app._validated_entity = SimpleNamespace(entity_name="Weekly sync")
            app.state = app.STATE_PREPARED
            app._run_batch_processing = MagicMock()
            app._build_prepared_audio_info = MagicMock(side_effect=AssertionError("should not rebuild"))

            spawned = []
            monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

            result = app.start_transcription()

            assert result["success"] is True
            assert wav_path.exists()
            assert app._session.context.wav_path == str(wav_path)
            assert spawned[0]._args[0].context.wav_path == str(wav_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_start_transcription_does_not_rename_non_placeholder_recording_stem(self, monkeypatch):
        root = _make_test_root("test_start_transcription_named_recording_no_rename")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            recordings_dir = app.data_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            wav_path = recordings_dir / "20260414_0930_Weekly-sync.wav"
            wav_path.write_bytes(b"wav")
            app._session = RecordingSession(SessionContext(
                wav_path=str(wav_path),
                compressed_path="",
                is_uploaded_file=False,
            ))
            app._prepared_audio_info = _prepared_audio_info_for_path(wav_path)
            app._validated_entity = SimpleNamespace(entity_name="Weekly sync")
            app.state = app.STATE_PREPARED
            app._run_batch_processing = MagicMock()
            app._build_prepared_audio_info = MagicMock(side_effect=AssertionError("should not rebuild"))

            spawned = []
            monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))

            result = app.start_transcription()

            assert result["success"] is True
            assert wav_path.exists()
            assert app._session.context.wav_path == str(wav_path)
            assert spawned[0]._args[0].context.wav_path == str(wav_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_start_transcription_continues_when_placeholder_recording_rename_fails(self, monkeypatch):
        root = _make_test_root("test_start_transcription_rename_failure")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            recordings_dir = app.data_dir / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            wav_path = recordings_dir / "20260414_0930_recording.wav"
            ogg_path = recordings_dir / "20260414_0930_recording.ogg"
            wav_path.write_bytes(b"wav")
            ogg_path.write_bytes(b"ogg")
            app._session = RecordingSession(SessionContext(
                wav_path=str(wav_path),
                compressed_path=str(ogg_path),
                is_uploaded_file=False,
            ))
            app._prepared_audio_info = _prepared_audio_info_for_path(wav_path)
            app._validated_entity = SimpleNamespace(entity_name="Weekly sync")
            app.state = app.STATE_PREPARED
            app._run_batch_processing = MagicMock()
            app._build_prepared_audio_info = MagicMock(side_effect=AssertionError("should not rebuild"))

            spawned = []
            monkeypatch.setattr("app.threading.Thread", _thread_spy(spawned))
            with patch.object(app, "_rename_paths_with_rollback", side_effect=OSError("blocked")):
                result = app.start_transcription()

            assert result["success"] is True
            assert wav_path.exists()
            assert ogg_path.exists()
            assert app._session.context.wav_path == str(wav_path)
            assert app._session.context.compressed_path == str(ogg_path)
            assert spawned[0]._args[0].context.wav_path == str(wav_path)
            assert spawned[0]._args[0].context.compressed_path == str(ogg_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestUndoableReplacement:
    def test_undo_session_replace_restores_stashed_workflow_and_discards_replacement_recording(self):
        app = _make_app()
        entity = SimpleNamespace(entity_name="Weekly sync", database="Internal Meeting")
        app.state = app.STATE_COMPLETED
        app._validated_entity = entity
        app._fibery_client = MagicMock()
        app._entity_context = {"company": "Fibery"}
        app._linked_transcript_text = "Existing Fibery transcript"
        app._transcript_mode = "replace"
        app._recording_mode = "mic_and_speakers"
        app._summary_mode = "replace"
        app._summary_language = "nl"
        app._prepared_audio_info = {"file_path": "meeting.wav", "file_name": "meeting.wav"}
        app._session = RecordingSession(SessionContext(entity=entity, wav_path="meeting.wav"))
        app._session.results.set_batch_result({
            "utterances": [{"speaker": "A", "text": "Pending transcript", "start": 0, "end": 1}]
        })
        app._session.results.set_cleaned_transcript("Pending transcript")
        app._session.results.set_generated_summary("Pending summary")

        stash = app.stash_session_undo_snapshot(15)

        assert stash == {"stored": True, "undo_available": True, "ttl_seconds": 15}

        app.reset_session_keep_meeting()
        app.state = app.STATE_RECORDING
        audio_capture = MagicMock()
        mixer = MagicMock()
        recorder = MagicMock()
        recorder.stop.return_value = Path("replacement.wav")
        recorder.compressed_path = "replacement.ogg"
        app.audio_capture = audio_capture
        app._mixer = mixer
        app._recorder = recorder
        app._session = RecordingSession(SessionContext(entity=entity, wav_path="replacement.wav"))
        app._release_recording_lock_async = MagicMock()

        snapshot = app.undo_session_replace()

        assert snapshot["state"] == app.STATE_COMPLETED
        assert snapshot["prepared_audio"]["file_path"] == "meeting.wav"
        assert snapshot["has_linked_meeting"] is True
        assert snapshot["entity_name"] == "Weekly sync"
        assert snapshot["entity_database"] == "Internal Meeting"
        assert snapshot["undo_available"] is False
        assert app._validated_entity is entity
        assert app._linked_transcript_text == "Existing Fibery transcript"
        assert app._transcript_mode == "replace"
        assert app._recording_mode == "mic_and_speakers"
        assert app._summary_mode == "replace"
        assert app._summary_language == "nl"
        assert app._prepared_audio_info["file_path"] == "meeting.wav"
        assert app._session is not None
        assert app._session.context.entity is entity
        assert app._session.results.get_cleaned_transcript() == "Pending transcript"
        assert app._session.results.get_generated_summary() == "Pending summary"
        audio_capture.stop_capture.assert_called_once_with()
        mixer.flush.assert_called_once_with()
        recorder.stop.assert_called_once_with()
        app._release_recording_lock_async.assert_called_once_with(entity)

    def test_undo_session_replace_rejects_expired_snapshot(self, monkeypatch):
        app = _make_app()
        entity = SimpleNamespace(entity_name="Weekly sync", database="Internal Meeting")
        app.state = app.STATE_COMPLETED
        app._validated_entity = entity
        app._session = RecordingSession(SessionContext(entity=entity, wav_path="meeting.wav"))

        app.stash_session_undo_snapshot(1)
        monkeypatch.setattr("app.time.monotonic", lambda: app._undo_snapshot_expires_at + 1)

        with pytest.raises(RuntimeError, match="No replacement session is available to undo."):
            app.undo_session_replace()

        assert app._undo_snapshot is None
        assert app.get_session_snapshot()["undo_available"] is False

    def test_undo_snapshot_does_not_restore_stale_inflight_guard_flags(self):
        app = _make_app()
        entity = SimpleNamespace(entity_name="Weekly sync", database="Internal Meeting")
        app.state = app.STATE_COMPLETED
        app._validated_entity = entity
        app._session = RecordingSession(SessionContext(entity=entity, wav_path="meeting.wav"))
        app._session.results.set_batch_result({
            "utterances": [{"speaker": "A", "text": "Pending transcript", "start": 0, "end": 1}]
        })
        app._session.results.try_start_transcript_send()
        app._session.results.try_start_summary_send()
        app._session.results.try_start_audio_upload()

        stash = app.stash_session_undo_snapshot(15)

        assert stash["stored"] is True
        snapshot = app.undo_session_replace()
        assert snapshot["state"] == app.STATE_COMPLETED
        assert app._session is not None
        assert app._session.results.try_start_transcript_send() is True
        assert app._session.results.try_start_summary_send() is True
        assert app._session.results.try_start_audio_upload() is True


class TestSessionBoundaryWorkers:
    def test_stale_transcript_auto_send_skips_fibery_write(self):
        app = _make_app()
        app.release_recording_lock = MagicMock()
        session = _make_pending_session()
        entity = SimpleNamespace(space="General", database="Internal Meeting")
        client = MagicMock()

        app.reset_session()
        stale_token = app._session_token - 1
        app._auto_send_transcript(entity, client, session, stale_token)

        client.update_transcript_only.assert_not_called()
        assert session.results.get_transcript_sent() is False

    def test_stale_audio_upload_skips_fibery_write(self):
        app = _make_app()
        app.release_recording_lock = MagicMock()
        session = RecordingSession(SessionContext(
            entity=SimpleNamespace(space="General", database="Internal Meeting"),
            fibery_client=MagicMock(),
            wav_path="meeting.wav",
        ))
        session.context.fibery_client.entity_supports_files.return_value = True

        app.reset_session()
        stale_token = app._session_token - 1
        result = app._upload_audio_to_fibery(session.context.wav_path, session, stale_token)

        assert result is False
        session.context.fibery_client.upload_file.assert_not_called()
        session.context.fibery_client.attach_file_to_entity.assert_not_called()
        assert session.results.get_audio_uploaded() is False

    def test_stale_pending_summary_auto_send_skips_fibery_write(self):
        app = _make_app()
        app.release_recording_lock = MagicMock()
        session = _make_pending_session()
        entity = SimpleNamespace(space="General", database="Internal Meeting")
        client = MagicMock()

        app.reset_session()
        stale_token = app._session_token - 1
        app._auto_send_pending_summary(entity, client, session, stale_token)

        client.update_summary_only.assert_not_called()
        assert session.results.get_summary_sent() is False


class TestUploadedFileStaging:
    def test_upload_and_transcribe_copies_external_file_to_default_recordings_dir(self):
        root = _make_test_root("test_uploaded_copy_default")
        try:
            app = _make_app(
                settings=Settings(display_name="Test", save_recordings=True),
                data_dir=root / "appdata",
            )
            app._validate_audio_file = MagicMock()
            app.stop_background_scanning = MagicMock()
            app.start_background_scanning = MagicMock()
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False

            source = root / "imports" / "meeting.mp3"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"audio-data")

            with patch("app.threading.Thread") as thread_cls:
                thread_cls.return_value = MagicMock()
                app.upload_and_transcribe(str(source))

            staged = Path(app._session.context.wav_path)
            assert staged.parent == app.data_dir / "recordings"
            assert re.fullmatch(r"\d{8}_\d{4}_recording_meeting\.mp3", staged.name)
            assert staged.read_bytes() == b"audio-data"
            assert source.exists()
            assert app._session.context.compressed_path == str(staged)
            thread_cls.return_value.start.assert_called_once_with()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_upload_and_transcribe_stages_m4a_as_compressed_upload_source(self):
        root = _make_test_root("test_uploaded_m4a_staging")
        try:
            app = _make_app(
                settings=Settings(display_name="Test", save_recordings=True),
                data_dir=root / "appdata",
            )
            app._validate_audio_file = MagicMock(return_value={
                "format": "M4A",
                "duration_seconds": 42.0,
                "sample_rate": 44100,
                "channels": 1,
                "size_bytes": 4096,
                "decoder_backend": "ffmpeg",
            })
            app.stop_background_scanning = MagicMock()
            app.start_background_scanning = MagicMock()
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False

            source = root / "imports" / "meeting.m4a"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"m4a" * 2048)

            with patch("app.threading.Thread") as thread_cls:
                thread_cls.return_value = MagicMock()
                app.upload_and_transcribe(str(source))

            staged = Path(app._session.context.wav_path)
            assert staged.parent == app.data_dir / "recordings"
            assert re.fullmatch(r"\d{8}_\d{4}_recording_meeting\.m4a", staged.name)
            assert staged.read_bytes() == source.read_bytes()
            assert app._session.context.compressed_path == str(staged)
            assert app._prepared_audio_info["file_name"] == staged.name
            assert app._prepared_audio_info["decoder_backend"] == "ffmpeg"
            thread_cls.return_value.start.assert_called_once_with()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_upload_and_transcribe_truncates_long_meeting_name_for_windows_safe_saved_path(self):
        root = _make_test_root("test_uploaded_copy_long_meeting_name")
        try:
            recordings_dir = root / "saved-audio" / "deep" / "folder" / "for" / "path" / "safety"
            app = _make_app(
                settings=Settings(display_name="Test", recordings_dir=str(recordings_dir), save_recordings=True),
                data_dir=root / "appdata",
            )
            app._validated_entity = SimpleNamespace(entity_name="Quarterly planning " * 40)
            app._validate_audio_file = MagicMock()
            app.stop_background_scanning = MagicMock()
            app.start_background_scanning = MagicMock()
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False

            source = root / "imports" / "meeting.wav"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"wav-data")

            with patch("app.threading.Thread") as thread_cls:
                thread_cls.return_value = MagicMock()
                app.upload_and_transcribe(str(source))

            staged = Path(app._session.context.wav_path)
            assert staged.exists()
            assert staged.parent == recordings_dir
            assert len(str(staged)) <= WINDOWS_SAFE_PATH_LIMIT
            assert staged.suffix == ".wav"
            assert staged.name.startswith("20")
            thread_cls.return_value.start.assert_called_once_with()
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestUploadedAudioValidation:
    def test_validate_audio_file_accepts_m4a_via_ffmpeg_decoder_fallback(self):
        root = _make_test_root("test_validate_m4a_decoder_fallback")
        try:
            app = _make_app(data_dir=root / "appdata")
            source = root / "imports" / "meeting.m4a"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"m4a" * 2048)
            fake_soundfile = SimpleNamespace(info=MagicMock(side_effect=RuntimeError("unsupported")))

            with patch.dict(sys.modules, {"soundfile": fake_soundfile}, clear=False):
                with patch("app.missing_ffmpeg_tools", return_value=[]):
                    with patch(
                        "app.load_audio_segment",
                        return_value=_FakeAudioSegment(duration_ms=2500, frame_rate=44100, channels=2),
                    ) as load_mock:
                        info = app._validate_audio_file(source)

            assert info["format"] == "M4A"
            assert info["duration_seconds"] == 2.5
            assert info["sample_rate"] == 44100
            assert info["channels"] == 2
            assert info["size_bytes"] == source.stat().st_size
            assert info["decoder_backend"] == "ffmpeg"
            load_mock.assert_called_once_with(source)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_validate_audio_file_rejects_m4a_when_ffmpeg_tools_are_missing(self):
        root = _make_test_root("test_validate_m4a_missing_ffmpeg")
        try:
            app = _make_app(data_dir=root / "appdata")
            source = root / "imports" / "meeting.m4a"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"m4a" * 2048)
            fake_soundfile = SimpleNamespace(info=MagicMock(side_effect=RuntimeError("unsupported")))

            with patch.dict(sys.modules, {"soundfile": fake_soundfile}, clear=False):
                with patch("app.missing_ffmpeg_tools", return_value=["ffmpeg", "ffprobe"]):
                    with pytest.raises(ValueError, match="M4A files because ffmpeg and ffprobe are not available"):
                        app._validate_audio_file(source)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestPreparedAudioClearing:
    def test_clear_prepared_recorded_audio_resets_to_idle_without_deleting_file(self):
        root = _make_test_root("test_clear_prepared_recorded_audio")
        try:
            app = _make_app(data_dir=root / "appdata")
            app._validate_audio_file = MagicMock(return_value={"channels": 1, "duration_seconds": 42})
            app._resume_background_scanning = MagicMock()
            app._validated_entity = "meeting-entity"

            recorded = app.data_dir / "recordings" / "meeting.wav"
            recorded.parent.mkdir(parents=True, exist_ok=True)
            recorded.write_bytes(b"recorded-audio")

            app._set_prepared_session(
                wav_path=str(recorded),
                compressed_path="",
                is_uploaded_file=False,
                entity="session-entity",
            )

            app.clear_prepared_audio()

            assert app.state == app.STATE_IDLE
            assert app._session is None
            assert app._prepared_audio_info is None
            assert app._validated_entity == "meeting-entity"
            assert recorded.exists()
            assert recorded.read_bytes() == b"recorded-audio"
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestRecorderLifecycle:
    def test_silence_checkpoint_restarts_recorder_with_current_mixer_channels(self):
        root = _make_test_root("test_recorder_checkpoint_channels")
        try:
            app = _make_app(
                settings=Settings(display_name="Test", noise_suppression=False, agc=False),
                data_dir=root / "appdata",
            )
            app.state = app.STATE_RECORDING
            app._mixer = MagicMock()
            app._mixer.channels = 1
            app._segment_start_time = 100.0
            app._recording_silence_start = None
            app._accumulated_recording_secs = 0.0
            app._recording_segments = []
            app._segment_ogg_paths = []
            app._checkpoints = []
            app._decision_popup_active = False
            app._notify_js = MagicMock()

            old_recorder = MagicMock()
            old_recorder.stop.return_value = root / "appdata" / "segment.wav"
            old_recorder.compressed_path = None
            app._recorder = old_recorder

            new_recorder = MagicMock()
            new_recorder.start.return_value = root / "appdata" / "recordings" / "next.wav"

            with patch("app.WavRecorder", return_value=new_recorder) as recorder_cls:
                with patch("app.time.monotonic", return_value=105.0):
                    app._save_milestone_segment("silence")

            assert recorder_cls.call_args.kwargs["channels"] == 1
            new_recorder.start.assert_called_once_with()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_resume_recording_restarts_recorder_with_rebuilt_mixer_channels(self):
        root = _make_test_root("test_recorder_resume_channels")
        try:
            app = _make_app(
                settings=Settings(display_name="Test", noise_suppression=False, agc=False),
                data_dir=root / "appdata",
            )
            app.audio_capture = MagicMock()
            app._selected_mic_index = 1
            app._selected_sys_index = None
            app._find_device = MagicMock(side_effect=[MagicMock(name="Mic"), None])

            new_recorder = MagicMock()
            new_recorder.start.return_value = root / "appdata" / "recordings" / "resume.wav"

            with patch("app.WavRecorder", return_value=new_recorder) as recorder_cls:
                with patch("app.time.monotonic", return_value=200.0):
                    app._resume_recording()

            assert recorder_cls.call_args.kwargs["channels"] == 1
            new_recorder.start.assert_called_once_with()
            assert app._mixer.channels == 1
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_resume_recording_preserves_session_channels_when_sources_change(self):
        root = _make_test_root("test_recorder_resume_preserves_session_channels")
        try:
            app = _make_app(
                settings=Settings(display_name="Test", noise_suppression=False, agc=False),
                data_dir=root / "appdata",
            )
            app.audio_capture = MagicMock()
            app._recording_channels = 2
            app._selected_mic_index = 1
            app._selected_sys_index = None
            app._find_device = MagicMock(side_effect=[MagicMock(name="Mic"), None])

            new_recorder = MagicMock()
            new_recorder.start.return_value = root / "appdata" / "recordings" / "resume.wav"

            with patch("app.WavRecorder", return_value=new_recorder) as recorder_cls:
                with patch("app.time.monotonic", return_value=200.0):
                    app._resume_recording()

            assert recorder_cls.call_args.kwargs["channels"] == 2
            assert app._mixer.channels == 2
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_upload_and_transcribe_uses_custom_recordings_dir_for_imported_files(self):
        root = _make_test_root("test_uploaded_copy_custom")
        try:
            recordings_dir = root / "saved-audio"
            app = _make_app(
                settings=Settings(display_name="Test", recordings_dir=str(recordings_dir), save_recordings=True),
                data_dir=root / "appdata",
            )
            app._validate_audio_file = MagicMock()
            app.stop_background_scanning = MagicMock()
            app.start_background_scanning = MagicMock()
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False

            source = root / "downloads" / "meeting.wav"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"wav-data")

            with patch("app.threading.Thread") as thread_cls:
                thread_cls.return_value = MagicMock()
                app.upload_and_transcribe(str(source))

            staged = Path(app._session.context.wav_path)
            assert staged.parent == recordings_dir
            assert re.fullmatch(r"\d{8}_\d{4}_recording_meeting\.wav", staged.name)
            assert staged.read_bytes() == b"wav-data"
            assert app._session.context.compressed_path == ""
            thread_cls.return_value.start.assert_called_once_with()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_upload_and_transcribe_keeps_file_in_place_when_already_in_recordings_dir(self):
        root = _make_test_root("test_uploaded_copy_skip")
        try:
            app = _make_app(data_dir=root / "appdata")
            app._validate_audio_file = MagicMock()
            app.stop_background_scanning = MagicMock()
            app.start_background_scanning = MagicMock()
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False

            recordings_dir = app.data_dir / "recordings"
            recordings_dir.mkdir(parents=True)
            source = recordings_dir / "meeting.mp3"
            source.write_bytes(b"audio-data")

            with patch("app.threading.Thread") as thread_cls:
                thread_cls.return_value = MagicMock()
                app.upload_and_transcribe(str(source))

            staged = Path(app._session.context.wav_path)
            assert staged == source
            assert list(recordings_dir.iterdir()) == [source]
            thread_cls.return_value.start.assert_called_once_with()
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestUploadedArtifactCleanup:
    def test_cleanup_uploaded_audio_artifacts_removes_generated_sidecars_only(self):
        root = _make_test_root("test_uploaded_artifact_cleanup")
        try:
            app = _make_app(data_dir=root / "appdata")
            source = root / "imports" / "meeting.wav"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"wav")

            generated = [
                source.with_suffix(".ogg"),
                source.with_suffix(".flac"),
                source.parent / "meeting_mono_input.wav",
                source.parent / "meeting_mono_input.ogg",
                source.parent / "meeting_processed.wav",
                source.parent / "meeting_processed.ogg",
            ]
            for artifact in generated:
                artifact.write_bytes(b"generated")

            app._cleanup_uploaded_audio_artifacts(str(source), str(generated[-1]))

            assert source.exists()
            for artifact in generated:
                assert not artifact.exists()
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestRecordedArtifactCleanup:
    def test_cleanup_recorded_audio_sidecars_keeps_raw_wav(self):
        root = _make_test_root("test_recorded_artifact_cleanup")
        try:
            app = _make_app(data_dir=root / "appdata")
            source = root / "recordings" / "meeting.wav"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"wav")

            generated = [
                source.with_suffix(".ogg"),
                source.with_suffix(".flac"),
                source.parent / "meeting_mono_input.wav",
                source.parent / "meeting_mono_input.ogg",
                source.parent / "meeting_processed.ogg",
                source.parent / "meeting_processed.flac",
            ]
            for artifact in generated:
                artifact.write_bytes(b"generated")

            app._cleanup_recorded_audio_sidecars(str(source), str(generated[-1]))

            assert source.exists()
            for artifact in generated:
                assert not artifact.exists()
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestRecordingBackgroundScanning:
    def test_start_recording_stops_background_scanning_before_capture(self):
        root = _make_test_root("test_start_recording_stops_background_scan")
        try:
            app = _make_app(
                settings=Settings(display_name="Test", noise_suppression=False, agc=False),
                data_dir=root / "appdata",
            )
            events: list[str] = []
            app.stop_background_scanning = MagicMock(side_effect=lambda: events.append("stop_scan"))
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False
            app.audio_capture.start_capture.side_effect = lambda **_kwargs: events.append("start_capture")
            app._find_device = MagicMock(side_effect=[MagicMock(name="Mic"), None])

            recorder = MagicMock()
            recorder.start.return_value = root / "appdata" / "recordings" / "segment.wav"

            with patch("app.WavRecorder", return_value=recorder):
                app.start_recording(1, None)

            assert events[:2] == ["stop_scan", "start_capture"]
            assert app.state == app.STATE_RECORDING
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_start_monitor_does_not_overlap_recording_start(self):
        root = _make_test_root("test_start_monitor_does_not_overlap_recording_start")
        try:
            app = _make_app(
                settings=Settings(display_name="Test", noise_suppression=False, agc=False),
                data_dir=root / "appdata",
            )
            app.stop_background_scanning = MagicMock()
            app.audio_capture = MagicMock()
            app.audio_capture.is_capturing.return_value = False

            capture_calls: list[dict] = []
            recording_capture_started = threading.Event()
            release_recording_capture = threading.Event()
            overlapping_monitor_capture = threading.Event()

            def start_capture(**kwargs):
                capture_calls.append(kwargs)
                if len(capture_calls) == 1:
                    recording_capture_started.set()
                    assert release_recording_capture.wait(timeout=2.0)
                else:
                    overlapping_monitor_capture.set()

            app.audio_capture.start_capture.side_effect = start_capture

            mic = SimpleNamespace(name="Mic")
            app._find_device = MagicMock(side_effect=lambda index, is_loopback: None if is_loopback else mic)

            recorder = MagicMock()
            recorder.start.return_value = root / "appdata" / "recordings" / "segment.wav"

            with patch("app.WavRecorder", return_value=recorder):
                recording_thread = threading.Thread(target=app.start_recording, args=(1, None))
                monitor_thread = threading.Thread(target=app.start_monitor, args=(1, None))

                recording_thread.start()
                assert recording_capture_started.wait(timeout=2.0)

                monitor_thread.start()
                assert not overlapping_monitor_capture.wait(timeout=0.2)

                release_recording_capture.set()
                recording_thread.join(timeout=2.0)
                monitor_thread.join(timeout=2.0)

            assert not recording_thread.is_alive()
            assert not monitor_thread.is_alive()
            assert len(capture_calls) == 1
            assert app.state == app.STATE_RECORDING
            app.audio_capture.stop_capture.assert_not_called()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_restarts_background_scanning_after_completion(self):
        root = _make_test_root("test_batch_processing_restarts_background_scan")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.state = app.STATE_PROCESSING
            app._resume_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            session = RecordingSession(SessionContext(wav_path=str(root / "meeting.wav")))

            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": "",
            }

            with patch("app.get_key", side_effect=lambda key: "assembly-key" if key == "assemblyai_api_key" else ""):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result):
                    app._run_batch_processing(session)

            app._resume_background_scanning.assert_called_once_with()
            assert app.state == app.STATE_COMPLETED
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_runs_text_only_gemini_cleanup_when_audio_attachment_disabled(self):
        from app import TranscriptionOptions

        root = _make_test_root("test_batch_processing_runs_text_only_gemini_cleanup")
        try:
            app = _make_app(
                settings=Settings(
                    display_name="Test",
                    audio_transcript_cleanup_enabled=False,
                ),
                data_dir=root / "appdata",
            )
            app.state = app.STATE_PROCESSING
            app._resume_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            session = RecordingSession(SessionContext(wav_path=str(root / "meeting.wav")))

            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": str(root / "meeting.ogg"),
            }
            (root / "meeting.ogg").write_bytes(b"ogg")
            (root / "meeting.ogg").write_bytes(b"ogg")

            def _get_key(name):
                if name == "assemblyai_api_key":
                    return "assembly-key"
                if name == "gemini_api_key":
                    return "gemini-key"
                return ""

            with patch("app.get_key", side_effect=_get_key):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result):
                    with patch(
                        "integrations.gemini_client.cleanup_transcript",
                        return_value="Cleaned transcript",
                    ) as cleanup:
                        app._run_batch_processing(
                            session,
                            TranscriptionOptions(improve_with_context=True),
                        )

            cleanup.assert_called_once()
            assert cleanup.call_args.kwargs["audio_path"] == ""
            app._resume_background_scanning.assert_called_once_with()
            assert app.state == app.STATE_COMPLETED
            assert session.results.get_cleaned_transcript() == "Cleaned transcript"
            messages = [call.args[0] for call in app._notify_js.call_args_list]
            assert "window.onCleanupFailed()" not in messages
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_runs_gemini_cleanup_when_enabled(self):
        from app import TranscriptionOptions

        root = _make_test_root("test_batch_processing_runs_gemini_cleanup")
        try:
            app = _make_app(
                settings=Settings(
                    display_name="Test",
                    audio_transcript_cleanup_enabled=True,
                ),
                data_dir=root / "appdata",
            )
            app.state = app.STATE_PROCESSING
            app.start_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            session = RecordingSession(SessionContext(wav_path=str(root / "meeting.wav")))

            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": str(root / "meeting.ogg"),
            }
            (root / "meeting.ogg").write_bytes(b"ogg")

            def _get_key(name):
                if name == "assemblyai_api_key":
                    return "assembly-key"
                if name == "gemini_api_key":
                    return "gemini-key"
                return ""

            with patch("app.get_key", side_effect=_get_key):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result):
                    with patch("integrations.gemini_client.cleanup_transcript", return_value="Cleaned transcript") as cleanup:
                        app._run_batch_processing(
                            session,
                            TranscriptionOptions(improve_with_context=True),
                        )

            cleanup.assert_called_once()
            assert cleanup.call_args.kwargs["audio_path"] == result["audio_path"]
            assert session.results.get_cleaned_transcript() == "Cleaned transcript"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_skips_gemini_cleanup_when_context_improvement_disabled(self):
        from app import TranscriptionOptions

        root = _make_test_root("test_batch_processing_skips_gemini_cleanup")
        try:
            app = _make_app(
                settings=Settings(
                    display_name="Test",
                    audio_transcript_cleanup_enabled=True,
                ),
                data_dir=root / "appdata",
            )
            app.state = app.STATE_PROCESSING
            app._resume_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            session = RecordingSession(SessionContext(wav_path=str(root / "meeting.wav")))

            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": str(root / "meeting.ogg"),
            }
            (root / "meeting.ogg").write_bytes(b"ogg")

            def _get_key(name):
                if name == "assemblyai_api_key":
                    return "assembly-key"
                if name == "gemini_api_key":
                    return "gemini-key"
                return ""

            with patch("app.get_key", side_effect=_get_key):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result):
                    with patch("integrations.gemini_client.cleanup_transcript") as cleanup:
                        app._run_batch_processing(
                            session,
                            TranscriptionOptions(improve_with_context=False),
                        )

            cleanup.assert_not_called()
            assert session.results.get_cleaned_transcript() == "**Speaker A**\nHello"
            messages = [call.args[0] for call in app._notify_js.call_args_list]
            assert "window.onCleanupFailed()" not in messages
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_logs_and_passes_curated_keyterms(self, caplog):
        root = _make_test_root("test_batch_processing_logs_keyterms")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.state = app.STATE_PROCESSING
            app._resume_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            session = RecordingSession(
                SessionContext(
                    wav_path=str(root / "meeting.wav"),
                    entity_context=EntityContext(
                        entity_name="Quarterly Review with Acme",
                        assignee_names=["Alice Johnson"],
                        people_names=["alice johnson", "Bob Stone"],
                        operator_names=["Carol Smith"],
                        organization_names=["Acme Holdings"],
                    ),
                )
            )
            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": "",
            }

            with patch("app.get_key", side_effect=lambda key: "assembly-key" if key == "assemblyai_api_key" else ""):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result) as transcribe:
                    with caplog.at_level(logging.INFO):
                        app._run_batch_processing(session)

            assert transcribe.call_args.kwargs["keyterms_prompt"] == [
                "Alice Johnson",
                "Bob Stone",
                "Carol Smith",
                "Acme Holdings",
            ]
            assert "AssemblyAI automatic keyterms applied: 4 phrases / 8 words" in caplog.text
            assert "filtered: duplicate=1" in caplog.text
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_logs_when_no_keyterms_survive_filtering(self, caplog):
        root = _make_test_root("test_batch_processing_logs_no_keyterms")
        try:
            app = _make_app(data_dir=root / "appdata")
            app.state = app.STATE_PROCESSING
            app._resume_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            session = RecordingSession(
                SessionContext(
                    wav_path=str(root / "meeting.wav"),
                    entity_context=EntityContext(
                        assignee_names=["Amy"],
                        organization_names=["IBM"],
                    ),
                )
            )
            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": "",
            }

            with patch("app.get_key", side_effect=lambda key: "assembly-key" if key == "assemblyai_api_key" else ""):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result) as transcribe:
                    with caplog.at_level(logging.INFO):
                        app._run_batch_processing(session)

            assert transcribe.call_args.kwargs["keyterms_prompt"] is None
            assert "AssemblyAI automatic keyterms not applied: no high-confidence candidates survived filtering" in caplog.text
            assert "unsupported_length=2" in caplog.text
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_removes_recorded_sidecars_when_save_recordings_off(self):
        root = _make_test_root("test_batch_processing_removes_recorded_sidecars")
        try:
            wav_path = root / "meeting.wav"
            wav_path.write_bytes(b"wav")
            raw_ogg = wav_path.with_suffix(".ogg")
            raw_flac = wav_path.with_suffix(".flac")
            processed_wav = wav_path.parent / "meeting_processed.wav"
            processed_ogg = wav_path.parent / "meeting_processed.ogg"
            processed_flac = wav_path.parent / "meeting_processed.flac"
            for artifact in (raw_ogg, raw_flac, processed_wav, processed_ogg, processed_flac):
                artifact.write_bytes(b"artifact")

            app = _make_app(
                settings=Settings(
                    display_name="Test",
                    save_recordings=False,
                    audio_storage="fibery",
                ),
                data_dir=root / "appdata",
            )
            app.state = app.STATE_PROCESSING
            app.start_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            app._upload_audio_to_fibery = MagicMock(return_value=True)
            session = RecordingSession(
                SessionContext(
                    wav_path=str(wav_path),
                    compressed_path=str(raw_ogg),
                )
            )
            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": str(processed_ogg),
            }

            with patch("app.get_key", side_effect=lambda key: "assembly-key" if key == "assemblyai_api_key" else ""):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result):
                    app._run_batch_processing(session)

            assert wav_path.exists()
            for artifact in (raw_ogg, raw_flac, processed_wav, processed_ogg, processed_flac):
                assert not artifact.exists()
            app._upload_audio_to_fibery.assert_called_once()
            assert app._upload_audio_to_fibery.call_args.args[0] == str(wav_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_processing_keeps_recorded_sidecars_when_save_recordings_on(self):
        root = _make_test_root("test_batch_processing_keeps_recorded_sidecars")
        try:
            wav_path = root / "meeting.wav"
            wav_path.write_bytes(b"wav")
            raw_ogg = wav_path.with_suffix(".ogg")
            raw_flac = wav_path.with_suffix(".flac")
            processed_wav = wav_path.parent / "meeting_processed.wav"
            processed_ogg = wav_path.parent / "meeting_processed.ogg"
            processed_flac = wav_path.parent / "meeting_processed.flac"
            for artifact in (raw_ogg, raw_flac, processed_wav, processed_ogg, processed_flac):
                artifact.write_bytes(b"artifact")

            app = _make_app(
                settings=Settings(
                    display_name="Test",
                    save_recordings=True,
                    audio_storage="fibery",
                ),
                data_dir=root / "appdata",
            )
            app.state = app.STATE_PROCESSING
            app.start_background_scanning = MagicMock()
            app._notify_js = MagicMock()
            app._upload_audio_to_fibery = MagicMock(return_value=True)
            session = RecordingSession(
                SessionContext(
                    wav_path=str(wav_path),
                    compressed_path=str(raw_ogg),
                )
            )
            result = {
                "utterances": [{"speaker": "A", "text": "Hello", "start": 0, "end": 1000}],
                "full_text": "Hello",
                "language": "en",
                "audio_path": str(processed_ogg),
            }

            with patch("app.get_key", side_effect=lambda key: "assembly-key" if key == "assemblyai_api_key" else ""):
                with patch("transcription.batch.transcribe_with_diarization", return_value=result):
                    app._run_batch_processing(session)

            assert wav_path.exists()
            assert raw_ogg.exists()
            assert raw_flac.exists()
            assert processed_ogg.exists()
            assert processed_flac.exists()
            assert not processed_wav.exists()
            app._upload_audio_to_fibery.assert_called_once()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_stop_recording_releases_lock_after_local_capture_stops(self):
        root = _make_test_root("test_stop_recording_releases_lock_after_capture")
        try:
            app = _make_app(data_dir=root / "appdata")
            events: list[str] = []
            app.state = app.STATE_RECORDING
            app._validated_entity = SimpleNamespace(entity_name="Weekly sync")
            app._fibery_client = MagicMock()
            app.audio_capture = MagicMock()
            app.audio_capture.stop_capture.side_effect = lambda: events.append("stop_capture")
            app._mixer = MagicMock()
            app._mixer.flush.side_effect = lambda: events.append("flush")

            recorder = MagicMock()
            recorder.stop.side_effect = lambda: events.append("recorder_stop") or (root / "appdata" / "segment.wav")
            recorder.compressed_path = None
            app._recorder = recorder
            app._session = RecordingSession(SessionContext())
            app._validate_audio_file = MagicMock(return_value={
                "format": "WAV",
                "duration_seconds": 5.0,
                "sample_rate": 16000,
                "channels": 1,
                "size_bytes": 2048,
            })
            app._finalize_segments = MagicMock(
                side_effect=lambda: events.append("finalize") or (str(root / "appdata" / "meeting.wav"), "")
            )
            app._release_recording_lock_async = MagicMock(
                side_effect=lambda entity: events.append("release_lock")
            )
            (root / "appdata").mkdir(parents=True, exist_ok=True)
            (root / "appdata" / "meeting.wav").write_bytes(b"wav")

            with patch("app.threading.Thread") as thread_cls:
                thread_cls.return_value = MagicMock()
                app.stop_recording()

            assert events.index("stop_capture") < events.index("release_lock")
            assert events.index("recorder_stop") < events.index("release_lock")
            assert app.state == app.STATE_PREPARED
            app._release_recording_lock_async.assert_called_once_with(app._validated_entity)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_upload_audio_to_fibery_supports_market_interview_entities(self):
        root = _make_test_root("test_upload_audio_to_fibery_market_interview")
        try:
            app = _make_app(data_dir=root / "appdata")
            wav_path = root / "market-interview.wav"
            wav_path.write_bytes(b"fake wav data")
            wav_path.with_suffix(".ogg").write_bytes(b"fake ogg data")
            app._notify_js = MagicMock()

            entity = SimpleNamespace(
                space="Market",
                database="Market Interview",
                uuid="entity-uuid",
            )
            client = MagicMock()
            client.entity_supports_files.return_value = True
            client.upload_file.return_value = {"fibery/id": "file-uuid"}

            session = RecordingSession(
                SessionContext(
                    entity=entity,
                    fibery_client=client,
                    wav_path=str(wav_path),
                )
            )

            ok = app._upload_audio_to_fibery(str(wav_path), session)

            assert ok is True
            client.entity_supports_files.assert_called_once_with(entity)
            client.upload_file.assert_called_once_with(wav_path)
            client.attach_file_to_entity.assert_called_once_with(entity, "file-uuid")
            assert wav_path.exists()
        finally:
            shutil.rmtree(root, ignore_errors=True)
