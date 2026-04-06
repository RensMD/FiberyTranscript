"""Application settings with JSON persistence."""

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_CLEANUP_MODEL = "gemini-3.1-flash-lite-preview"
_RETIRED_CLEANUP_MODELS = {"gemini-2.5-flash-lite"}


@dataclass
class Settings:
    # Audio preferences
    preferred_mic_device: str = ""
    preferred_loopback_device: str = ""

    # App behavior
    auto_start_on_boot: bool = False
    minimize_to_tray_on_close: bool = True
    theme: str = "dark"  # "light" or "dark"

    # Recording-time cleanup for the original OGG copy
    noise_suppression: bool = True
    agc: bool = True

    # Post-processing before upload/transcription (opt-in)
    audio_transcript_cleanup_enabled: bool = False
    post_processing: bool = False
    echo_cancellation: bool = False
    post_noise_suppression: bool = False
    post_agc: bool = False
    post_normalize: bool = False

    # Recording
    save_recordings: bool = True
    recordings_dir: str = ""
    audio_storage: str = "local"  # "local" or "fibery"

    # AI models
    gemini_model: str = "gemini-3.1-pro-preview"
    gemini_model_fallback: str = "gemini-3-flash-preview"
    gemini_model_cleanup: str = DEFAULT_CLEANUP_MODEL

    # Summarization
    company_context: str = ""

    # User identity (for recording lock)
    display_name: str = ""

    # Default page URL for the Fibery entity panel
    default_panel_page: str = ""

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """Load settings from JSON file, or return defaults if not found."""
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                known_fields = cls.__dataclass_fields__
                filtered = {k: v for k, v in data.items() if k in known_fields}
                settings = cls(**filtered)
                if settings.gemini_model_cleanup in _RETIRED_CLEANUP_MODELS:
                    settings.gemini_model_cleanup = DEFAULT_CLEANUP_MODEL
                return settings
            except (json.JSONDecodeError, TypeError, AttributeError):
                return cls()
        return cls()

    def save(self, path: Path) -> None:
        """Persist settings to JSON file atomically (write-then-rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(str(tmp), str(path))

    def merge_installer_prefs(self, data_dir: Path) -> bool:
        """Merge installer_prefs.json into settings and delete the file.

        The Windows installer writes this file with user-configured values.
        On first launch after install/upgrade, we merge them into settings
        so the user's choices take effect without overwriting unrelated settings.

        Returns True if prefs were merged.
        """
        prefs_path = data_dir / "installer_prefs.json"
        if not prefs_path.exists():
            return False

        try:
            with open(prefs_path, encoding="utf-8") as f:
                prefs = json.load(f)
        except (json.JSONDecodeError, OSError):
            prefs_path.unlink(missing_ok=True)
            return False

        known_fields = self.__dataclass_fields__
        merged = False
        for key, value in prefs.items():
            if key in known_fields:
                setattr(self, key, value)
                merged = True

        # Delete the prefs file so it's not re-applied
        prefs_path.unlink(missing_ok=True)
        return merged
