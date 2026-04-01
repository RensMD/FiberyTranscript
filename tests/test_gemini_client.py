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
    )

    assert cleaned == "cleaned transcript"
    client = fake_google.Client.instances[0]
    assert client.http_options == {"timeout": gemini_client._CLEANUP_REQUEST_TIMEOUT_MS}
    assert [call["model"] for call in client.calls] == [
        "gemini-cleanup",
        "gemini-3.1-flash-lite-preview",
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
