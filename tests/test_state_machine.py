"""Tests for FiberyTranscriptApp state machine: transitions, guards, close confirmation."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

from config.settings import Settings
from session import RecordingSession, SessionContext, SessionResults


def _make_app():
    """Create a minimal FiberyTranscriptApp for state testing."""
    from app import FiberyTranscriptApp
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(display_name="Test")
    app = FiberyTranscriptApp(settings, tmp)
    return app


class TestStateConstants:
    def test_state_values(self):
        from app import FiberyTranscriptApp
        assert FiberyTranscriptApp.STATE_IDLE == "idle"
        assert FiberyTranscriptApp.STATE_RECORDING == "recording"
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
        app._recording_segments = [Path("seg1.wav")]
        app._sleeping = True

        app.reset_session()

        assert app.state == "idle"
        assert app._session is None
        assert app._validated_entity is None
        assert app._entity_context is None
        assert app._recording_segments == []
        assert app._sleeping is False
