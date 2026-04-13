"""Shared helpers for decoding uploaded audio across supported formats."""

from __future__ import annotations

import shutil
from pathlib import Path

SOUNDFILE_NATIVE_AUDIO_EXTENSIONS = {
    ".wav",
    ".ogg",
    ".flac",
}

FFMPEG_BACKED_AUDIO_FORMATS = {
    ".mp3": "mp3",
    ".m4a": "mp4",
    ".aac": "aac",
    ".wma": "asf",
    ".webm": "webm",
}

SUPPORTED_UPLOADED_AUDIO_EXTENSIONS = (
    SOUNDFILE_NATIVE_AUDIO_EXTENSIONS | set(FFMPEG_BACKED_AUDIO_FORMATS)
)


def missing_ffmpeg_tools() -> list[str]:
    """Return any external decoder tools required by pydub that are missing."""
    missing: list[str] = []
    if not (shutil.which("ffmpeg") or shutil.which("avconv")):
        missing.append("ffmpeg")
    if not (shutil.which("ffprobe") or shutil.which("avprobe")):
        missing.append("ffprobe")
    return missing


def load_audio_segment(audio_path: str | Path):
    """Load audio through pydub, using an explicit format hint when helpful."""
    from pydub import AudioSegment

    path = Path(audio_path)
    format_hint = FFMPEG_BACKED_AUDIO_FORMATS.get(path.suffix.lower())
    if format_hint:
        return AudioSegment.from_file(str(path), format=format_hint)
    return AudioSegment.from_file(str(path))
