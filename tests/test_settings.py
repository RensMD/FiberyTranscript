"""Tests for Settings: load, save, defaults, missing fields, corruption."""

import json
from pathlib import Path
from config.settings import Settings


class TestSettingsDefaults:
    def test_default_values(self):
        s = Settings()
        assert s.preferred_mic_device == ""
        assert s.auto_start_on_boot is False
        assert s.minimize_to_tray_on_close is True
        assert s.theme == "dark"
        assert s.noise_suppression is True
        assert s.agc is True
        assert s.audio_transcript_cleanup_enabled is False
        assert s.post_processing is False
        assert s.echo_cancellation is False
        assert s.post_noise_suppression is False
        assert s.post_agc is False
        assert s.post_normalize is False
        assert s.save_recordings is True
        assert s.audio_storage == "local"
        assert s.display_name == ""

    def test_custom_values(self):
        s = Settings(display_name="Alice", theme="light", save_recordings=False)
        assert s.display_name == "Alice"
        assert s.theme == "light"
        assert s.save_recordings is False


class TestSettingsLoad:
    def test_load_missing_file(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        s = Settings.load(path)
        assert s.theme == "dark"  # defaults

    def test_load_valid(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"display_name": "Bob", "theme": "light"}))
        s = Settings.load(path)
        assert s.display_name == "Bob"
        assert s.theme == "light"
        # Other fields keep defaults
        assert s.save_recordings is True
        assert s.audio_transcript_cleanup_enabled is False
        assert s.post_processing is False
        assert s.echo_cancellation is False
        assert s.post_noise_suppression is False
        assert s.post_agc is False
        assert s.post_normalize is False

    def test_load_ignores_unknown_fields(self, tmp_path):
        """Unknown keys in JSON should not cause errors."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"display_name": "X", "unknown_field": 42}))
        s = Settings.load(path)
        assert s.display_name == "X"
        assert not hasattr(s, "unknown_field")

    def test_load_corrupted_json(self, tmp_path):
        """Corrupted JSON falls back to defaults."""
        path = tmp_path / "settings.json"
        path.write_text("{invalid json!!!")
        s = Settings.load(path)
        assert s.theme == "dark"

    def test_load_wrong_type(self, tmp_path):
        """Non-dict JSON (e.g., a list) falls back to defaults."""
        path = tmp_path / "settings.json"
        path.write_text("[1, 2, 3]")
        s = Settings.load(path)
        assert s.theme == "dark"


class TestSettingsSave:
    def test_atomic_write(self, tmp_path):
        """Save uses write-then-rename (no .tmp left behind)."""
        path = tmp_path / "settings.json"
        s = Settings(display_name="Alice")
        s.save(path)
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()
        loaded = json.loads(path.read_text())
        assert loaded["display_name"] == "Alice"

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "settings.json"
        s = Settings()
        s.save(path)
        assert path.exists()

    def test_round_trip(self, tmp_path):
        """Save then load should preserve all fields."""
        path = tmp_path / "settings.json"
        original = Settings(
            display_name="Test",
            theme="light",
            save_recordings=False,
            gemini_model="custom-model",
            company_context="Test context",
            audio_transcript_cleanup_enabled=True,
            post_processing=True,
            echo_cancellation=True,
            post_noise_suppression=True,
            post_agc=True,
            post_normalize=True,
        )
        original.save(path)
        loaded = Settings.load(path)
        assert loaded.display_name == original.display_name
        assert loaded.theme == original.theme
        assert loaded.save_recordings == original.save_recordings
        assert loaded.gemini_model == original.gemini_model
        assert loaded.company_context == original.company_context
        assert loaded.audio_transcript_cleanup_enabled == original.audio_transcript_cleanup_enabled
        assert loaded.post_processing == original.post_processing
        assert loaded.echo_cancellation == original.echo_cancellation
        assert loaded.post_noise_suppression == original.post_noise_suppression
        assert loaded.post_agc == original.post_agc
        assert loaded.post_normalize == original.post_normalize
