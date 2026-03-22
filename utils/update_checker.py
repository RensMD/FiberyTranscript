"""Background update checker using GitHub Releases API."""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# GitHub API endpoint for latest release
_RELEASES_URL = "https://api.github.com/repos/RensMD/FiberyTranscript/releases/latest"


def _parse_version(tag: str) -> tuple[int, ...]:
    """Parse a version tag like 'v1.3.0' or '1.3.0' into a comparable tuple."""
    tag = tag.lstrip("vV").strip()
    parts = []
    for part in tag.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            break
    return tuple(parts)


def check_for_update(current_version: str) -> Optional[dict]:
    """Check GitHub for a newer release.

    Returns a dict with 'version', 'url', and 'notes' if an update is
    available, or None if already up-to-date or on error.
    """
    import urllib.request
    import json

    try:
        req = urllib.request.Request(
            _RELEASES_URL,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "FiberyTranscript-UpdateChecker",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("Update check failed: %s", e)
        return None

    tag = data.get("tag_name", "")
    latest = _parse_version(tag)
    current = _parse_version(current_version)

    if not latest or not current:
        return None

    if latest > current:
        return {
            "version": tag.lstrip("vV"),
            "url": data.get("html_url", ""),
            "notes": data.get("body", ""),
        }
    return None


def check_for_update_async(current_version: str, callback) -> None:
    """Check for updates in a background thread.

    Calls callback(result) where result is the dict from check_for_update
    or None. The callback is called from the background thread.
    """
    def _worker():
        result = check_for_update(current_version)
        if result:
            logger.info("Update available: %s -> %s", current_version, result["version"])
        callback(result)

    t = threading.Thread(target=_worker, daemon=True, name="update-checker")
    t.start()
