"""Shared helpers for recording and staged-audio filenames."""

from datetime import datetime
from pathlib import Path
import re

WINDOWS_SAFE_PATH_LIMIT = 240
MIN_STEM_LENGTH = 32
FILENAME_HEADROOM = len("_mono_input") + len("_2147483647") + len(".flac")

# Accept current yyyymmdd_hhmm_, legacy yyyy-mm-dd_hh_mm_ / yyyy-mm-dd_hh-mm_, and older date-only form.
RECORDING_PREFIX_RE = re.compile(r"^(?:\d{8}|\d{4}-\d{2}-\d{2})(?:_\d{2,4}[-_]?\d{0,2})?_")
PLACEHOLDER_RECORDING_STEM_RE = re.compile(
    r"^(?P<merged>merged_)?(?P<prefix>(?:\d{8}|\d{4}-\d{2}-\d{2})(?:_\d{2,4}[-_]?\d{0,2})?)_recording(?P<counter>_\d+)?$"
)


def sanitize_name(name: str) -> str:
    """Sanitize a name for use in filenames."""
    name = name.strip().replace(" ", "-")
    name = re.sub(r"[^\w\-.]", "", name)
    name = re.sub(r"[-_]{2,}", "-", name)
    name = name.strip("-_.")
    return name or "recording"


def truncate_stem_for_directory(stem: str, directory: Path, suffix: str) -> str:
    """Trim long stems so the path and later sidecars stay Windows-safe."""
    available = (
        WINDOWS_SAFE_PATH_LIMIT
        - len(str(directory))
        - 1  # path separator
        - len(suffix)
        - FILENAME_HEADROOM
    )
    available = max(MIN_STEM_LENGTH, available)
    if len(stem) <= available:
        return stem

    truncated = stem[:available].rstrip("._-")
    return truncated or stem[:available]


def build_recording_stem(name: str = "", *, now: datetime | None = None) -> str:
    """Build the default recording stem using yyyymmdd_hhmm and a sanitized name."""
    moment = now or datetime.now()
    prefix = moment.strftime("%Y%m%d_%H%M")
    safe_name = sanitize_name(name) if name else "recording"
    return f"{prefix}_{safe_name}"


def append_counter(base_stem: str, counter: int | None) -> str:
    """Append a numeric suffix when one is required."""
    if counter is None:
        return base_stem
    return f"{base_stem}_{counter}"
