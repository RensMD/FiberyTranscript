import sys
from types import ModuleType, SimpleNamespace

from integrations import gemini_client


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeModels:
    def __init__(self, responses, calls):
        self._responses = list(responses)
        self._calls = calls

    def generate_content(self, **kwargs):
        self._calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)


class _FakeClient:
    responses = []
    instances = []
    upload_result = None
    upload_calls = []
    delete_calls = []

    def __init__(self, api_key, http_options):
        self.api_key = api_key
        self.http_options = http_options
        self.calls = []
        self.models = _FakeModels(type(self).responses, self.calls)
        self.files = SimpleNamespace(
            upload=self._upload,
            delete=self._delete,
        )
        type(self).instances.append(self)

    @classmethod
    def _upload(cls, **kwargs):
        cls.upload_calls.append(kwargs)
        return cls.upload_result

    @classmethod
    def _delete(cls, **kwargs):
        cls.delete_calls.append(kwargs)
        return None


def _install_fake_google(monkeypatch, responses):
    class DeadlineExceeded(Exception):
        pass

    class GatewayTimeout(Exception):
        pass

    class NotFound(Exception):
        pass

    class ResourceExhausted(Exception):
        pass

    class ServiceUnavailable(Exception):
        pass

    class TooManyRequests(Exception):
        pass

    google_module = ModuleType("google")
    genai_module = ModuleType("google.genai")
    types_module = ModuleType("google.genai.types")
    api_core_module = ModuleType("google.api_core")
    exceptions_module = ModuleType("google.api_core.exceptions")

    _FakeClient.responses = list(responses)
    _FakeClient.instances = []
    _FakeClient.upload_result = None
    _FakeClient.upload_calls = []
    _FakeClient.delete_calls = []

    genai_module.Client = _FakeClient
    genai_module.types = types_module
    types_module.GenerateContentConfig = _FakeGenerateContentConfig

    exceptions_module.DeadlineExceeded = DeadlineExceeded
    exceptions_module.GatewayTimeout = GatewayTimeout
    exceptions_module.NotFound = NotFound
    exceptions_module.ResourceExhausted = ResourceExhausted
    exceptions_module.ServiceUnavailable = ServiceUnavailable
    exceptions_module.TooManyRequests = TooManyRequests

    google_module.genai = genai_module
    google_module.api_core = api_core_module
    api_core_module.exceptions = exceptions_module

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_module)
    monkeypatch.setitem(sys.modules, "google.api_core", api_core_module)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exceptions_module)

    return SimpleNamespace(
        Client=_FakeClient,
        DeadlineExceeded=DeadlineExceeded,
    )


def test_retryable_classifier_matches_deadline_message():
    assert gemini_client._is_retryable_gemini_error(Exception("504 Deadline Exceeded"))
    assert gemini_client._is_retryable_gemini_error(Exception("deadline exceeded while generating"))


def test_summarize_transcript_falls_back_on_deadline_exceeded(monkeypatch):
    fake_google = _install_fake_google(
        monkeypatch,
        [
            Exception("504 Deadline Exceeded"),
            "fallback summary",
        ],
    )

    summary = gemini_client.summarize_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        notes="",
        is_interview=False,
        model="gemini-pro",
        model_fallback="gemini-flash",
    )

    assert summary == "fallback summary"
    client = fake_google.Client.instances[0]
    assert client.http_options == {"timeout": gemini_client._SUMMARY_REQUEST_TIMEOUT_MS}
    assert [call["model"] for call in client.calls] == ["gemini-pro", "gemini-flash"]


def test_summarize_transcript_short_interview_style_adds_tighter_length_limits(monkeypatch):
    fake_google = _install_fake_google(monkeypatch, ["short summary"])

    summary = gemini_client.summarize_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        notes="",
        is_interview=True,
        summary_style="short",
        model="gemini-pro",
        model_fallback="gemini-flash",
    )

    assert summary == "short summary"
    client = fake_google.Client.instances[0]
    call = client.calls[0]
    system_instruction = call["config"].kwargs["system_instruction"]
    assert "Summary style setting: short" in system_instruction
    assert "Keep it under about 900 characters" in system_instruction
    assert "at most 2 problem definition suggestions" in system_instruction


def test_summarize_transcript_uses_requested_output_language(monkeypatch):
    fake_google = _install_fake_google(monkeypatch, ["Nederlandse samenvatting"])

    summary = gemini_client.summarize_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        notes="",
        is_interview=False,
        summary_language="nl",
        model="gemini-pro",
        model_fallback="gemini-flash",
    )

    assert summary == "Nederlandse samenvatting"
    client = fake_google.Client.instances[0]
    system_instruction = client.calls[0]["config"].kwargs["system_instruction"]
    assert "Output language: Dutch." in system_instruction
    assert "Write the entire summary in Dutch." in system_instruction


def test_summarize_transcript_single_prompt_type_uses_meeting_prompt(monkeypatch):
    from config.constants import DEFAULT_MEETING_PROMPT

    fake_google = _install_fake_google(monkeypatch, ["meeting summary"])
    summary = gemini_client.summarize_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        notes="",
        prompt_types=["summarize"],
        model="gemini-pro",
        model_fallback="gemini-flash",
    )

    assert summary == "meeting summary"
    call = fake_google.Client.instances[0].calls[0]
    assert DEFAULT_MEETING_PROMPT in call["config"].kwargs["system_instruction"]


def test_summarize_transcript_interview_prompt_type_uses_interview_prompt(monkeypatch):
    from config.constants import DEFAULT_INTERVIEW_PROMPT

    fake_google = _install_fake_google(monkeypatch, ["interview summary"])
    summary = gemini_client.summarize_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        notes="",
        prompt_types=["interview"],
        model="gemini-pro",
        model_fallback="gemini-flash",
    )

    assert summary == "interview summary"
    call = fake_google.Client.instances[0].calls[0]
    assert DEFAULT_INTERVIEW_PROMPT[:50] in call["config"].kwargs["system_instruction"]


def test_summarize_transcript_multi_prompt_types_makes_separate_calls_and_combines(monkeypatch):
    fake_google = _install_fake_google(monkeypatch, ["meeting text", "shareable text"])
    summary = gemini_client.summarize_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        notes="",
        prompt_types=["summarize", "shareable"],
        model="gemini-pro",
        model_fallback="gemini-flash",
    )

    client = fake_google.Client.instances[0]
    assert len(client.calls) == 2
    assert "---" in summary
    assert "## Summary" in summary
    assert "## Shareable Summary" in summary
    assert "meeting text" in summary
    assert "shareable text" in summary


def test_summarize_transcript_custom_prompt_type_uses_custom_text(monkeypatch):
    fake_google = _install_fake_google(monkeypatch, ["custom summary"])
    summary = gemini_client.summarize_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        notes="",
        prompt_types=["custom"],
        custom_prompt="My special instructions",
        model="gemini-pro",
        model_fallback="gemini-flash",
    )

    assert summary == "custom summary"
    call = fake_google.Client.instances[0].calls[0]
    assert "My special instructions" in call["config"].kwargs["system_instruction"]


def test_cleanup_transcript_falls_back_on_deadline_exceeded(monkeypatch):
    fake_google = _install_fake_google(
        monkeypatch,
        [
            Exception("504 Deadline Exceeded"),
            "cleaned transcript",
        ],
    )

    cleaned = gemini_client.cleanup_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        model="gemini-cleanup",
        model_fallback="gemini-flash",
    )

    assert cleaned == "cleaned transcript"
    client = fake_google.Client.instances[0]
    assert client.http_options == {"timeout": gemini_client._CLEANUP_REQUEST_TIMEOUT_MS}
    assert [call["model"] for call in client.calls] == [
        "gemini-cleanup",
        "gemini-flash",
    ]


def test_cleanup_transcript_deletes_uploaded_audio_in_background(monkeypatch, tmp_path):
    fake_google = _install_fake_google(monkeypatch, ["cleaned transcript"])
    fake_google.Client.upload_result = SimpleNamespace(name="files/uploaded-audio")

    started_threads = []

    class _ImmediateThread:
        def __init__(self, *, target, args=(), kwargs=None, name=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self.name = name
            self.daemon = daemon
            started_threads.append(self)

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(gemini_client.threading, "Thread", _ImmediateThread)

    audio_path = tmp_path / "sample.ogg"
    audio_path.write_bytes(b"ogg-data")

    cleaned = gemini_client.cleanup_transcript(
        api_key="test-key",
        transcript="Speaker A: hello",
        model="gemini-cleanup",
        model_fallback="gemini-flash",
        audio_path=str(audio_path),
    )

    assert cleaned == "cleaned transcript"
    assert fake_google.Client.upload_calls == [
        {"file": str(audio_path), "config": {"mime_type": "audio/ogg"}}
    ]
    assert fake_google.Client.delete_calls == [{"name": "files/uploaded-audio"}]
    assert len(started_threads) == 1
    assert started_threads[0].daemon is True
    assert started_threads[0].name == "gemini-file-delete"

    assert len(fake_google.Client.instances) == 2
    assert fake_google.Client.instances[0].http_options == {
        "timeout": gemini_client._CLEANUP_REQUEST_TIMEOUT_MS
    }
    assert fake_google.Client.instances[1].http_options == {
        "timeout": gemini_client._FILE_DELETE_TIMEOUT_MS
    }


def test_cleanup_transcript_treats_company_context_as_glossary_not_attendance(monkeypatch):
    fake_google = _install_fake_google(monkeypatch, ["cleaned transcript"])

    cleaned = gemini_client.cleanup_transcript(
        api_key="test-key",
        transcript="**Speaker A**\nHello there",
        notes="Participants likely include Rens and Andrej.",
        meeting_context="Confirmed internal participants in this meeting: Rens\nConfirmed external participants in this meeting: Andrej Karpathy",
        company_context="Possible people at the company: Alice Example, Bob Example",
        model="gemini-cleanup",
        model_fallback="gemini-flash",
    )

    assert cleaned == "cleaned transcript"
    client = fake_google.Client.instances[0]
    call = client.calls[0]
    system_instruction = call["config"].kwargs["system_instruction"]
    assert "The detected transcript language is English." in system_instruction
    assert "Keep the entire output in English." in system_instruction
    assert "Only replace a generic speaker label with a real name" in system_instruction
    assert "Never assign a speaker name based only on general company context" in system_instruction
    assert "keep the source version and remove the echoed duplicate" in system_instruction
    assert "remove the duplicated portion and keep the unique remainder" in system_instruction
    assert "prefer keeping Channel 1 and removing the duplicate Channel 0 text" in system_instruction
    assert "Do not summarize, condense, paraphrase" in system_instruction
    assert "Keep every substantive statement" in system_instruction
    assert "Do not add section summaries" in system_instruction
    assert "Confirmed meeting-specific context:" in system_instruction
    assert "General company context (glossary only; not evidence that a person attended this meeting):" in system_instruction
    assert "Participants likely include Rens and Andrej." in call["contents"]


def test_cleanup_output_is_suspiciously_short_only_for_long_major_reductions():
    long_source = " ".join(["word"] * 700)
    deduped = " ".join(["word"] * 320)
    summarized = " ".join(["word"] * 180)
    short_source = " ".join(["word"] * 60)

    assert not gemini_client._cleanup_output_is_suspiciously_short(long_source, deduped)
    assert gemini_client._cleanup_output_is_suspiciously_short(long_source, summarized)
    assert not gemini_client._cleanup_output_is_suspiciously_short(short_source, "short summary")


def test_cleanup_transcript_uses_raw_when_all_models_overcompress(monkeypatch):
    fake_google = _install_fake_google(
        monkeypatch,
        [
            "short summary",
            "still too short",
        ],
    )

    transcript = " ".join(["full"] * 700)
    cleaned = gemini_client.cleanup_transcript(
        api_key="test-key",
        transcript=transcript,
        model="gemini-cleanup",
        model_fallback="gemini-flash",
    )

    assert cleaned == transcript
    client = fake_google.Client.instances[0]
    assert [call["model"] for call in client.calls] == [
        "gemini-cleanup",
        "gemini-flash",
    ]


def test_split_transcript_for_cleanup_preserves_speaker_blocks():
    transcript = (
        "**Speaker 0**\nHello there.\n\n"
        "**Speaker 1**\nHi.\n\n"
        "**Speaker 0**\nLet's continue.\n\n"
        "**Speaker 1**\nSounds good."
    )

    chunks = gemini_client._split_transcript_for_cleanup(transcript, max_chars=60, max_blocks=2)

    assert chunks == [
        "**Speaker 0**\nHello there.\n\n**Speaker 1**\nHi.",
        "**Speaker 0**\nLet's continue.\n\n**Speaker 1**\nSounds good.",
    ]


def test_cleanup_transcript_splits_long_transcripts_into_multiple_requests(monkeypatch):
    fake_google = _install_fake_google(
        monkeypatch,
        [
            "cleaned chunk one",
            "cleaned chunk two",
        ],
    )
    monkeypatch.setattr(gemini_client, "_CLEANUP_MAX_CHARS_PER_REQUEST", 60)
    monkeypatch.setattr(gemini_client, "_CLEANUP_MAX_BLOCKS_PER_REQUEST", 2)

    transcript = (
        "**Speaker 0**\nHello there.\n\n"
        "**Speaker 1**\nHi.\n\n"
        "**Speaker 0**\nLet's continue.\n\n"
        "**Speaker 1**\nSounds good."
    )

    cleaned = gemini_client.cleanup_transcript(
        api_key="test-key",
        transcript=transcript,
        notes="Meeting context note.",
        model="gemini-cleanup",
        model_fallback="gemini-flash",
    )

    assert cleaned == "cleaned chunk one\n\ncleaned chunk two"
    client = fake_google.Client.instances[0]
    assert len(client.calls) == 2
    assert "Transcript chunk 1 of 2" in client.calls[0]["contents"]
    assert "Transcript chunk 2 of 2" in client.calls[1]["contents"]


def test_cleanup_transcript_uses_detected_language_without_translation(monkeypatch):
    fake_google = _install_fake_google(monkeypatch, ["Schoongemaakte transcriptie"])

    cleaned = gemini_client.cleanup_transcript(
        api_key="test-key",
        transcript="**Spreker 0**\nHallo daar",
        language="nl",
        model="gemini-cleanup",
        model_fallback="gemini-flash",
    )

    assert cleaned == "Schoongemaakte transcriptie"
    client = fake_google.Client.instances[0]
    system_instruction = client.calls[0]["config"].kwargs["system_instruction"]
    assert "The detected transcript language is Dutch." in system_instruction
    assert "Keep the entire output in Dutch." in system_instruction
    assert "Never translate" in system_instruction
