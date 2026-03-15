"""Secure API key storage using the system keyring.

Keys are stored in the platform-native credential store:
  - Windows: Credential Locker
  - macOS: Keychain
  - Linux: Secret Service (GNOME Keyring / KWallet)

Environment variables take priority (useful for CI or team distribution):
  FIBERY_TRANSCRIPT_ASSEMBLYAI_KEY
  FIBERY_TRANSCRIPT_GEMINI_KEY
  FIBERY_TRANSCRIPT_FIBERY_TOKEN
"""

import logging
import os

logger = logging.getLogger(__name__)

_SERVICE_NAME = "FiberyTranscript"

_KEY_NAMES = {
    "assemblyai_api_key": "FIBERY_TRANSCRIPT_ASSEMBLYAI_KEY",
    "gemini_api_key": "FIBERY_TRANSCRIPT_GEMINI_KEY",
    "fibery_api_token": "FIBERY_TRANSCRIPT_FIBERY_TOKEN",
}


def _get_keyring():
    """Import keyring, returning None if unavailable."""
    try:
        import keyring
        return keyring
    except ImportError:
        logger.warning("keyring package not installed, falling back to env vars only")
        return None


_SECRETS_MAP = {
    "assemblyai_api_key": "ASSEMBLYAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "fibery_api_token": "FIBERY_API_TOKEN",
}


def _get_from_secrets_file(name: str) -> str:
    """Fallback: try importing from config/secrets.py (dev-only, gitignored)."""
    attr = _SECRETS_MAP.get(name)
    if not attr:
        return ""
    try:
        from config import secrets
        return getattr(secrets, attr, "") or ""
    except ImportError:
        return ""


def get_key(name: str) -> str:
    """Retrieve an API key by name.

    Lookup order:
      1. Environment variable
      2. System keyring
      3. config/secrets.py (dev fallback, gitignored)
      4. Empty string (not configured)
    """
    env_var = _KEY_NAMES.get(name)
    if env_var:
        value = os.environ.get(env_var, "")
        if value:
            return value

    kr = _get_keyring()
    if kr:
        try:
            value = kr.get_password(_SERVICE_NAME, name)
            if value:
                return value
        except Exception as e:
            logger.warning("Keyring read failed for %s: %s", name, e)

    # Dev fallback: check config/secrets.py
    value = _get_from_secrets_file(name)
    if value:
        return value

    return ""


def set_key(name: str, value: str) -> bool:
    """Store an API key in the system keyring. Returns True on success."""
    kr = _get_keyring()
    if not kr:
        return False
    try:
        kr.set_password(_SERVICE_NAME, name, value)
        return True
    except Exception as e:
        logger.error("Keyring write failed for %s: %s", name, e)
        return False


def delete_key(name: str) -> bool:
    """Remove an API key from the system keyring. Returns True on success."""
    kr = _get_keyring()
    if not kr:
        return False
    try:
        kr.delete_password(_SERVICE_NAME, name)
        return True
    except Exception as e:
        logger.warning("Keyring delete failed for %s: %s", name, e)
        return False


def get_all_keys() -> dict:
    """Return all API keys as a dict. Values may be empty if not configured."""
    return {
        "assemblyai_api_key": get_key("assemblyai_api_key"),
        "gemini_api_key": get_key("gemini_api_key"),
        "fibery_api_token": get_key("fibery_api_token"),
    }


def save_all_keys(keys: dict) -> bool:
    """Save all API keys to keyring. Returns True if all succeeded."""
    success = True
    for name in ("assemblyai_api_key", "gemini_api_key", "fibery_api_token"):
        value = keys.get(name, "")
        if value:
            if not set_key(name, value):
                success = False
    return success


def keys_configured() -> bool:
    """Check if all required API keys are configured."""
    keys = get_all_keys()
    return all(keys.values())
