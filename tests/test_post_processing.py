import sys
import shutil
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

from audio.post_processor import PostProcessor
from config.settings import Settings
from transcription import batch


def _make_app(settings: Settings | None = None):
    from app import FiberyTranscriptApp

    tmp = Path(tempfile.mkdtemp())
    app = FiberyTranscriptApp(settings or Settings(display_name="Test"), tmp)
    return app


def _make_test_root(name: str) -> Path:
    root = Path.cwd() / "data" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fake_assemblyai(calls: dict):
    class _FakeTranscriber:
        def upload_file(self, path):
            calls["upload_path"] = path
            return "upload://audio"

        def transcribe(self, upload_url, config):
            calls["transcribe_upload_url"] = upload_url
            calls["config"] = getattr(config, "kwargs", {})
            return types.SimpleNamespace(
                status="completed",
                utterances=[],
                text="ok",
                language_code="en",
            )

    return types.SimpleNamespace(
        settings=types.SimpleNamespace(api_key=None),
        TranscriptStatus=types.SimpleNamespace(error="error"),
        TranscriptionConfig=lambda **kwargs: types.SimpleNamespace(kwargs=kwargs),
        Transcriber=lambda: _FakeTranscriber(),
    )


def test_app_builds_post_only_processing_settings():
    app = _make_app(Settings(
        display_name="Test",
        noise_suppression=False,
        agc=False,
        post_processing=True,
        echo_cancellation=True,
        post_noise_suppression=True,
        post_agc=True,
        post_normalize=True,
    ))

    assert app._build_post_process_settings() == {
        "echo_cancel": True,
        "noise_suppress": True,
        "agc": True,
        "normalize": True,
    }


def test_app_returns_no_post_process_settings_when_disabled():
    app = _make_app(Settings(
        display_name="Test",
        post_processing=False,
        echo_cancellation=True,
        post_noise_suppression=True,
        post_agc=True,
        post_normalize=True,
    ))

    assert app._build_post_process_settings() is None


def test_transcribe_uses_precompressed_audio_when_post_processing_is_off():
    root = _make_test_root("test_post_processing_precompressed")
    try:
        wav_path = root / "meeting.wav"
        ogg_path = root / "meeting.ogg"
        wav_path.write_bytes(b"wav")
        ogg_path.write_bytes(b"ogg")
        calls = {}

        fake_soundfile = types.SimpleNamespace(
            info=lambda _path: types.SimpleNamespace(channels=2),
        )
        fake_normalizer = types.ModuleType("audio.normalizer")
        fake_normalizer.normalize_audio = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("normalize_audio should not run when post-processing is off")
        )

        with patch.dict(sys.modules, {
            "assemblyai": _fake_assemblyai(calls),
            "soundfile": fake_soundfile,
            "audio.normalizer": fake_normalizer,
        }, clear=False):
            with patch.object(batch, "_compress_audio", side_effect=AssertionError("unexpected compression")):
                result = batch.transcribe_with_diarization(
                    api_key="test-key",
                    audio_path=str(wav_path),
                    compressed_path=str(ogg_path),
                    post_process=False,
                )

        assert calls["upload_path"] == str(ogg_path)
        assert calls["config"]["multichannel"] is True
        assert calls["config"]["speaker_labels"] is True
        assert calls["config"]["speech_models"] == ["universal-3-pro", "universal-2"]
        assert calls["config"]["language_detection"] is True
        assert result["audio_path"] == str(ogg_path)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_transcribe_passes_full_post_process_settings():
    root = _make_test_root("test_post_processing_full_settings")
    try:
        wav_path = root / "meeting.wav"
        processed_wav = root / "meeting_processed.wav"
        processed_ogg = root / "meeting_processed.ogg"
        wav_path.write_bytes(b"wav")
        processed_wav.write_bytes(b"processed-wav")
        processed_ogg.write_bytes(b"processed-ogg")
        calls = {}
        captured_kwargs = {}

        class _FakePostProcessor:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            def process(self, raw_wav_path, on_progress=None):
                calls["processed_input"] = str(raw_wav_path)
                if on_progress is not None:
                    on_progress("Enhancing audio...")
                return processed_wav

        fake_soundfile = types.SimpleNamespace(
            info=lambda _path: types.SimpleNamespace(channels=2),
        )
        fake_post_processor = types.ModuleType("audio.post_processor")
        fake_post_processor.PostProcessor = _FakePostProcessor
        stage_settings = {
            "echo_cancel": True,
            "noise_suppress": False,
            "agc": True,
            "normalize": True,
        }

        with patch.dict(sys.modules, {
            "assemblyai": _fake_assemblyai(calls),
            "soundfile": fake_soundfile,
            "audio.post_processor": fake_post_processor,
        }, clear=False):
            with patch.object(batch, "_compress_audio", return_value=str(processed_ogg)) as compress_mock:
                result = batch.transcribe_with_diarization(
                    api_key="test-key",
                    audio_path=str(wav_path),
                    post_process=True,
                    post_process_settings=stage_settings,
                )

        compress_mock.assert_called_once_with(str(processed_wav))
        assert captured_kwargs == stage_settings
        assert calls["processed_input"] == str(wav_path)
        assert calls["config"]["multichannel"] is True
        assert calls["config"]["speaker_labels"] is True
        assert calls["upload_path"] == str(processed_ogg)
        assert result["audio_path"] == str(processed_ogg)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_transcribe_keeps_precompressed_fallback_when_post_processor_returns_original():
    root = _make_test_root("test_post_processing_preserves_fallback")
    try:
        wav_path = root / "meeting.wav"
        ogg_path = root / "meeting.ogg"
        wav_path.write_bytes(b"wav")
        ogg_path.write_bytes(b"ogg")
        calls = {}

        class _FakePostProcessor:
            def __init__(self, **_kwargs):
                pass

            def process(self, raw_wav_path, on_progress=None):
                if on_progress is not None:
                    on_progress("Enhancing audio...")
                return raw_wav_path

        fake_soundfile = types.SimpleNamespace(
            info=lambda _path: types.SimpleNamespace(channels=2),
        )
        fake_post_processor = types.ModuleType("audio.post_processor")
        fake_post_processor.PostProcessor = _FakePostProcessor

        with patch.dict(sys.modules, {
            "assemblyai": _fake_assemblyai(calls),
            "soundfile": fake_soundfile,
            "audio.post_processor": fake_post_processor,
        }, clear=False):
            with patch.object(batch, "_compress_audio", side_effect=AssertionError("unexpected compression")):
                result = batch.transcribe_with_diarization(
                    api_key="test-key",
                    audio_path=str(wav_path),
                    compressed_path=str(ogg_path),
                    post_process=True,
                )

        assert calls["upload_path"] == str(ogg_path)
        assert result["audio_path"] == str(ogg_path)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_transcribe_downmixes_stereo_for_mic_only_mode():
    root = _make_test_root("test_post_processing_downmixes_mic_only")
    try:
        wav_path = root / "meeting.wav"
        wav_path.write_bytes(b"wav")
        mono_wav = root / "meeting_mono_input.wav"
        mono_wav.write_bytes(b"mono")
        mono_ogg = root / "meeting_mono_input.ogg"
        mono_ogg.write_bytes(b"mono-ogg")
        calls = {}

        fake_soundfile = types.SimpleNamespace(
            info=lambda path: types.SimpleNamespace(channels=2 if str(path) == str(wav_path) else 1),
        )

        with patch.dict(sys.modules, {
            "assemblyai": _fake_assemblyai(calls),
            "soundfile": fake_soundfile,
        }, clear=False):
            with patch.object(batch, "_downmix_to_mono_wav", return_value=str(mono_wav)) as downmix_mock:
                with patch.object(batch, "_compress_audio", return_value=str(mono_ogg)) as compress_mock:
                    result = batch.transcribe_with_diarization(
                        api_key="test-key",
                        audio_path=str(wav_path),
                        post_process=False,
                        recording_mode="mic_only",
                    )

        downmix_mock.assert_called_once_with(str(wav_path))
        compress_mock.assert_called_once_with(str(mono_wav))
        assert calls["upload_path"] == str(mono_ogg)
        assert "multichannel" not in calls["config"]
        assert result["effective_recording_mode"] == "mic_only"
        assert result["audio_path"] == str(mono_ogg)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_transcribe_keeps_multichannel_for_distinct_speakers_mode():
    root = _make_test_root("test_post_processing_keeps_multichannel")
    try:
        wav_path = root / "meeting.wav"
        ogg_path = root / "meeting.ogg"
        wav_path.write_bytes(b"wav")
        ogg_path.write_bytes(b"ogg")
        calls = {}

        fake_soundfile = types.SimpleNamespace(
            info=lambda _path: types.SimpleNamespace(channels=2),
        )

        with patch.dict(sys.modules, {
            "assemblyai": _fake_assemblyai(calls),
            "soundfile": fake_soundfile,
        }, clear=False):
            with patch.object(batch, "_downmix_to_mono_wav", side_effect=AssertionError("unexpected downmix")):
                result = batch.transcribe_with_diarization(
                    api_key="test-key",
                    audio_path=str(wav_path),
                    compressed_path=str(ogg_path),
                    post_process=False,
                    recording_mode="mic_and_speakers",
                )

        assert calls["upload_path"] == str(ogg_path)
        assert calls["config"]["multichannel"] is True
        assert result["effective_recording_mode"] == "mic_and_speakers"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_multichannel_transcription_skips_speaker_identification_hints():
    config = batch._build_config_kwargs(
        word_boost=["Andrej", "Google"],
        speaker_hints={
            "speaker_options": {
                "min_speakers_expected": 2,
                "max_speakers_expected": 3,
                "use_two_stage_clustering": True,
            },
            "speaker_identification": ["Andrej Karpathy", "Dwarkesh Patel"],
        },
        multichannel=True,
    )

    assert config["multichannel"] is True
    assert config["speaker_labels"] is True
    assert config["speaker_options"] == {
        "min_speakers_expected": 2,
        "max_speakers_expected": 3,
        "use_two_stage_clustering": True,
    }
    assert "speech_understanding" not in config


def test_mono_transcription_keeps_speaker_identification_hints():
    config = batch._build_config_kwargs(
        word_boost=None,
        speaker_hints={
            "speakers_expected": 2,
            "speaker_identification": ["Andrej Karpathy", "Dwarkesh Patel"],
        },
        multichannel=False,
    )

    assert config["speakers_expected"] == 2
    assert config["speech_understanding"] == {
        "request": {
            "speaker_identification": {
                "speaker_type": "name",
                "known_values": ["Andrej Karpathy", "Dwarkesh Patel"],
            }
        }
    }


def test_post_processor_returns_original_file_when_no_stage_changes_audio():
    root = _make_test_root("test_post_processor_noop_returns_original")
    try:
        source = root / "meeting.mp3"
        source.write_bytes(b"mp3-audio")

        fake_soundfile = types.SimpleNamespace(
            info=lambda _path: types.SimpleNamespace(channels=2),
        )

        with patch.dict(sys.modules, {"soundfile": fake_soundfile}, clear=False):
            processor = PostProcessor(
                echo_cancel=True,
                noise_suppress=False,
                agc=False,
                normalize=False,
            )
            with patch.object(processor, "_run_echo_cancellation", return_value=source):
                result = processor.process(source)

        assert result == source
        assert not (root / "meeting_processed.wav").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_echo_dedupe_drops_lower_confidence_mic_duplicate():
    utterances = [
        {
            "speaker": "A",
            "text": "That model will probably have to look it up.",
            "start": 1000,
            "end": 2500,
            "channel": 0,
            "confidence": 0.71,
        },
        {
            "speaker": "B",
            "text": "That model will probably have to look it up.",
            "start": 1080,
            "end": 2480,
            "channel": 1,
            "confidence": 0.92,
        },
    ]

    result = batch._suppress_echo_duplicates(utterances)

    assert len(result) == 1
    assert result[0]["channel"] == 1


def test_echo_dedupe_keeps_distinct_mic_speech():
    utterances = [
        {
            "speaker": "A",
            "text": "Can you hear me now?",
            "start": 1000,
            "end": 1800,
            "channel": 0,
            "confidence": 0.95,
        },
        {
            "speaker": "B",
            "text": "Let's move to the next question.",
            "start": 1120,
            "end": 2500,
            "channel": 1,
            "confidence": 0.91,
        },
    ]

    result = batch._suppress_echo_duplicates(utterances)

    assert len(result) == 2


def test_echo_dedupe_requires_true_time_overlap():
    utterances = [
        {
            "speaker": "A",
            "text": "That model will probably have to look it up.",
            "start": 1000,
            "end": 1800,
            "channel": 0,
            "confidence": 0.70,
        },
        {
            "speaker": "B",
            "text": "That model will probably have to look it up.",
            "start": 1820,
            "end": 2600,
            "channel": 1,
            "confidence": 0.95,
        },
    ]

    result = batch._suppress_echo_duplicates(utterances)

    assert len(result) == 2


def test_echo_dedupe_requires_near_exact_duplicate_text():
    utterances = [
        {
            "speaker": "A",
            "text": "That model might have to look a few things up.",
            "start": 1000,
            "end": 2200,
            "channel": 0,
            "confidence": 0.70,
        },
        {
            "speaker": "B",
            "text": "That model will probably have to look it up.",
            "start": 1080,
            "end": 2280,
            "channel": 1,
            "confidence": 0.95,
        },
    ]

    result = batch._suppress_echo_duplicates(utterances)

    assert len(result) == 2


def test_echo_dedupe_requires_meaningful_confidence_gap():
    utterances = [
        {
            "speaker": "A",
            "text": "That model will probably have to look it up.",
            "start": 1000,
            "end": 2200,
            "channel": 0,
            "confidence": 0.83,
        },
        {
            "speaker": "B",
            "text": "That model will probably have to look it up.",
            "start": 1070,
            "end": 2190,
            "channel": 1,
            "confidence": 0.90,
        },
    ]

    result = batch._suppress_echo_duplicates(utterances)

    assert len(result) == 2


def test_echo_dedupe_never_suppresses_long_mic_utterances():
    utterances = [
        {
            "speaker": "A",
            "text": "I think the important thing here is that the model should know when it does not know something and then look it up carefully.",
            "start": 1000,
            "end": 4200,
            "channel": 0,
            "confidence": 0.60,
        },
        {
            "speaker": "B",
            "text": "I think the important thing here is that the model should know when it does not know something and then look it up carefully.",
            "start": 1090,
            "end": 4180,
            "channel": 1,
            "confidence": 0.94,
        },
    ]

    result = batch._suppress_echo_duplicates(utterances)

    assert len(result) == 2
