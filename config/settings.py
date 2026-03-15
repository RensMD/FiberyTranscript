"""Application settings with JSON persistence."""

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class Settings:
    # Audio preferences
    preferred_mic_device: str = ""
    preferred_loopback_device: str = ""

    # App behavior
    auto_start_on_boot: bool = False
    minimize_to_tray_on_close: bool = True
    theme: str = "dark"  # "light" or "dark"

    # Recording
    save_recordings: bool = True
    recordings_dir: str = ""
    audio_storage: str = "local"  # "local" or "fibery"

    # AI models
    gemini_model: str = "gemini-3.1-pro-preview"
    gemini_model_fallback: str = "gemini-3-flash-preview"
    gemini_model_cleanup: str = "gemini-2.0-flash"

    # Summarization
    company_context: str = ""

    # User identity (for recording lock)
    display_name: str = ""

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """Load settings from JSON file, or return defaults if not found."""
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                known_fields = cls.__dataclass_fields__
                filtered = {k: v for k, v in data.items() if k in known_fields}
                return cls(**filtered)
            except (json.JSONDecodeError, TypeError):
                return cls()
        return cls()

    def save(self, path: Path) -> None:
        """Persist settings to JSON file atomically (write-then-rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(str(tmp), str(path))
