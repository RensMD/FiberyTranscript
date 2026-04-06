"""Recording session model.

A RecordingSession is created at start_recording() and cleared at reset_session().
It splits session data into two parts:
  - SessionContext  : frozen at recording start, safe to read from any thread
  - SessionResults  : mutable results written by background threads, all access via lock
"""

import threading
from dataclasses import dataclass
from typing import Optional, Any


@dataclass(frozen=True)
class SessionContext:
    """Frozen at recording start. Never modified after creation."""

    entity: Optional[Any] = None          # FiberyEntity snapshot
    fibery_client: Optional[Any] = None   # FiberyClient snapshot
    entity_context: Optional[dict] = None # Word-boost / summary context snapshot
    wav_path: str = ""
    compressed_path: str = ""
    is_uploaded_file: bool = False


class SessionResults:
    """Mutable results. All reads and writes must go through the accessor methods."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Core results
        self._batch_result: Optional[dict] = None
        self._cleaned_transcript: Optional[str] = None
        self._generated_summary: Optional[str] = None

        # Completion flags
        self._transcript_sent: bool = False
        self._summary_sent: bool = False
        self._user_has_copied: bool = False
        self._audio_uploaded: bool = False

        # In-flight guard flags (Decision E — prevent duplicate sends)
        self._transcript_sending: bool = False
        self._summary_sending: bool = False
        self._audio_uploading: bool = False

    # --- Batch result ---

    def set_batch_result(self, result: dict) -> None:
        with self._lock:
            self._batch_result = result

    def get_batch_result(self) -> Optional[dict]:
        with self._lock:
            return self._batch_result

    # --- Transcripts ---

    def set_cleaned_transcript(self, text: str) -> None:
        with self._lock:
            self._cleaned_transcript = text

    def get_cleaned_transcript(self) -> Optional[str]:
        with self._lock:
            return self._cleaned_transcript

    def reset_transcription_outputs(self) -> None:
        """Clear transcript + summary artifacts before retranscribing the same audio."""
        with self._lock:
            self._batch_result = None
            self._cleaned_transcript = None
            self._generated_summary = None
            self._transcript_sent = False
            self._summary_sent = False
            self._user_has_copied = False
            self._transcript_sending = False
            self._summary_sending = False

    # --- Summary ---

    def set_generated_summary(self, text: str) -> None:
        with self._lock:
            self._generated_summary = text

    def get_generated_summary(self) -> Optional[str]:
        with self._lock:
            return self._generated_summary

    # --- Completion flags ---

    def get_transcript_sent(self) -> bool:
        with self._lock:
            return self._transcript_sent

    def get_summary_sent(self) -> bool:
        with self._lock:
            return self._summary_sent

    def get_audio_uploaded(self) -> bool:
        with self._lock:
            return self._audio_uploaded

    def set_user_has_copied(self) -> None:
        with self._lock:
            self._user_has_copied = True

    def get_user_has_copied(self) -> bool:
        with self._lock:
            return self._user_has_copied

    # --- Guard flags (idempotent send protection) ---

    def try_start_transcript_send(self) -> bool:
        """Returns True if this thread may proceed with the send (not already in-flight)."""
        with self._lock:
            if self._transcript_sending:
                return False
            self._transcript_sending = True
            return True

    def finish_transcript_send(self, success: bool) -> None:
        with self._lock:
            self._transcript_sending = False
            if success:
                self._transcript_sent = True

    def try_start_summary_send(self) -> bool:
        with self._lock:
            if self._summary_sending:
                return False
            self._summary_sending = True
            return True

    def finish_summary_send(self, success: bool) -> None:
        with self._lock:
            self._summary_sending = False
            if success:
                self._summary_sent = True

    def try_start_audio_upload(self) -> bool:
        with self._lock:
            if self._audio_uploading:
                return False
            self._audio_uploading = True
            return True

    def finish_audio_upload(self, success: bool = True) -> None:
        with self._lock:
            self._audio_uploading = False
            if success:
                self._audio_uploaded = True


class RecordingSession:
    """Active recording session. Created at start_recording(), cleared at reset_session()."""

    def __init__(self, context: SessionContext) -> None:
        self.context = context        # frozen — read freely from any thread
        self.results = SessionResults()  # lock-protected
