"""Tests for recording lock format v2: build, parse, ownership, staleness."""

import socket
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from config.settings import Settings


def _make_app():
    """Create a minimal FiberyTranscriptApp for lock testing."""
    from app import FiberyTranscriptApp
    from pathlib import Path
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    settings = Settings(display_name="Alice")
    app = FiberyTranscriptApp(settings, tmp)
    return app


class TestBuildLockValue:
    def test_format_v2(self):
        app = _make_app()
        lock = app._build_lock_value()
        # Format: Name@Host|ISO-timestamp
        assert "@" in lock
        assert "|" in lock
        name_host, ts = lock.rsplit("|", 1)
        assert name_host.startswith("Alice@")
        # Timestamp should be parseable
        datetime.fromisoformat(ts)

    def test_uses_display_name(self):
        app = _make_app()
        app.settings = Settings(display_name="  Bob  ")
        lock = app._build_lock_value()
        assert lock.startswith("Bob@")

    def test_falls_back_to_username(self):
        app = _make_app()
        app.settings = Settings(display_name="")
        lock = app._build_lock_value()
        # Should not start with @ (username should be non-empty)
        name_host = lock.rsplit("|", 1)[0]
        assert not name_host.startswith("@")


class TestParseLock:
    def test_v2_format(self):
        app = _make_app()
        ts = datetime.now(timezone.utc).isoformat()
        name, host, timestamp = app._parse_lock(f"Alice@DESKTOP-123|{ts}")
        assert name == "Alice"
        assert host == "DESKTOP-123"
        assert timestamp is not None

    def test_legacy_name_only(self):
        app = _make_app()
        name, host, timestamp = app._parse_lock("OldUser")
        assert name == "OldUser"
        assert host == ""
        assert timestamp is None

    def test_legacy_name_with_timestamp(self):
        app = _make_app()
        ts = datetime.now(timezone.utc).isoformat()
        name, host, timestamp = app._parse_lock(f"OldUser|{ts}")
        assert name == "OldUser"
        assert host == ""
        assert timestamp is not None

    def test_email_style_name_not_split(self):
        """Legacy locks with email-like names should not split on @."""
        app = _make_app()
        name, host, timestamp = app._parse_lock("alice@example.com")
        # 'example.com' has a dot → treated as legacy email, not host
        assert name == "alice@example.com"
        assert host == ""

    def test_email_with_timestamp(self):
        app = _make_app()
        ts = "2026-03-15T10:00:00+00:00"
        name, host, timestamp = app._parse_lock(f"alice@example.com|{ts}")
        assert name == "alice@example.com"
        assert host == ""
        assert timestamp is not None

    def test_invalid_timestamp_ignored(self):
        app = _make_app()
        name, host, timestamp = app._parse_lock("Alice@HOST|not-a-date")
        assert name == "Alice"
        assert host == "HOST"
        assert timestamp is None

    def test_whitespace_stripped(self):
        app = _make_app()
        ts = datetime.now(timezone.utc).isoformat()
        name, host, timestamp = app._parse_lock(f"  Alice @ HOST | {ts} ")
        # rsplit then strip — spaces around the @ end up in name/host
        # The method strips outer whitespace and name/host individually
        assert name == "Alice"  # name_part.strip()
        assert host == "HOST"   # host_part.strip() — but wait, "@ HOST" not "HOST"...

    def test_empty_string(self):
        app = _make_app()
        name, host, timestamp = app._parse_lock("")
        assert name == ""
        assert host == ""
        assert timestamp is None


class TestCheckRecordingLock:
    def test_no_entity_returns_unlocked(self):
        app = _make_app()
        app._validated_entity = None
        result = app.check_recording_lock()
        assert result == {"locked": False}

    def test_empty_lock_returns_unlocked(self):
        app = _make_app()
        app._validated_entity = MagicMock()
        app._fibery_client = MagicMock()
        app._fibery_client.get_recording_lock.return_value = ""
        result = app.check_recording_lock()
        assert result["locked"] is False

    def test_own_lock_returns_unlocked(self):
        app = _make_app()
        app.settings = Settings(display_name="Alice")
        my_host = socket.gethostname()
        ts = datetime.now(timezone.utc).isoformat()
        app._validated_entity = MagicMock()
        app._fibery_client = MagicMock()
        app._fibery_client.get_recording_lock.return_value = f"Alice@{my_host}|{ts}"
        result = app.check_recording_lock()
        assert result["locked"] is False

    def test_other_user_lock_returns_locked(self):
        app = _make_app()
        app.settings = Settings(display_name="Alice")
        ts = datetime.now(timezone.utc).isoformat()
        app._validated_entity = MagicMock()
        app._fibery_client = MagicMock()
        app._fibery_client.get_recording_lock.return_value = f"Bob@OTHERPC|{ts}"
        result = app.check_recording_lock()
        assert result["locked"] is True
        assert "Bob" in result["locked_by"]

    def test_stale_lock_ignored(self):
        """Locks older than 30 minutes are treated as stale."""
        app = _make_app()
        app.settings = Settings(display_name="Alice")
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        app._validated_entity = MagicMock()
        app._fibery_client = MagicMock()
        app._fibery_client.get_recording_lock.return_value = f"Bob@OTHERPC|{old_ts}"
        result = app.check_recording_lock()
        assert result["locked"] is False
