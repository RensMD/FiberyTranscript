"""Tests for SessionContext, SessionResults, and RecordingSession."""

import threading
from session import RecordingSession, SessionContext, SessionResults


class TestSessionContext:
    def test_frozen(self):
        ctx = SessionContext(entity="e", wav_path="/tmp/test.wav")
        try:
            ctx.entity = "other"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_defaults(self):
        ctx = SessionContext()
        assert ctx.entity is None
        assert ctx.fibery_client is None
        assert ctx.wav_path == ""
        assert ctx.compressed_path == ""
        assert ctx.is_uploaded_file is False


class TestSessionResults:
    def test_batch_result(self):
        r = SessionResults()
        assert r.get_batch_result() is None
        r.set_batch_result({"utterances": []})
        assert r.get_batch_result() == {"utterances": []}

    def test_cleaned_transcript(self):
        r = SessionResults()
        assert r.get_cleaned_transcript() is None
        r.set_cleaned_transcript("hello world")
        assert r.get_cleaned_transcript() == "hello world"

    def test_generated_summary(self):
        r = SessionResults()
        assert r.get_generated_summary() is None
        r.set_generated_summary("summary text")
        assert r.get_generated_summary() == "summary text"

    def test_user_has_copied(self):
        r = SessionResults()
        assert r.get_user_has_copied() is False
        r.set_user_has_copied()
        assert r.get_user_has_copied() is True

    def test_transcript_guard_flags(self):
        r = SessionResults()
        # First try succeeds
        assert r.try_start_transcript_send() is True
        # Second try fails (in-flight)
        assert r.try_start_transcript_send() is False
        # Finish with failure — can retry
        r.finish_transcript_send(success=False)
        assert r.get_transcript_sent() is False
        assert r.try_start_transcript_send() is True
        # Finish with success
        r.finish_transcript_send(success=True)
        assert r.get_transcript_sent() is True

    def test_summary_guard_flags(self):
        r = SessionResults()
        assert r.try_start_summary_send() is True
        assert r.try_start_summary_send() is False
        r.finish_summary_send(success=True)
        assert r.get_summary_sent() is True

    def test_audio_upload_guard(self):
        r = SessionResults()
        assert r.try_start_audio_upload() is True
        assert r.try_start_audio_upload() is False
        r.finish_audio_upload()
        assert r.try_start_audio_upload() is True

    def test_concurrent_access(self):
        """Multiple threads can safely read/write results."""
        r = SessionResults()
        errors = []

        def writer():
            try:
                for i in range(100):
                    r.set_batch_result({"i": i})
                    r.set_cleaned_transcript(f"text-{i}")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    r.get_batch_result()
                    r.get_cleaned_transcript()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestRecordingSession:
    def test_creation(self):
        ctx = SessionContext(entity="test", wav_path="/tmp/test.wav")
        session = RecordingSession(ctx)
        assert session.context is ctx
        assert session.results is not None
        assert session.results.get_batch_result() is None
