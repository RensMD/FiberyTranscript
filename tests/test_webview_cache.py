import json
import shutil
from pathlib import Path

from utils.webview_cache import refresh_main_webview_cache_if_needed


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_test_dir(name: str) -> Path:
    path = PROJECT_ROOT / "codex_tmp" / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_refresh_main_webview_cache_clears_only_volatile_dirs():
    data_dir = _make_test_dir("test_webview_cache_refresh")
    try:
        volatile_dir = data_dir / "webview_storage" / "EBWebView" / "Default" / "Cache"
        volatile_dir.mkdir(parents=True)
        (volatile_dir / "cached.js").write_text("old", encoding="utf-8")

        local_storage_dir = data_dir / "webview_storage" / "EBWebView" / "Default" / "Local Storage"
        local_storage_dir.mkdir(parents=True)
        (local_storage_dir / "persisted.txt").write_text("keep", encoding="utf-8")

        panel_dir = data_dir / "webview_storage" / "fibery_panel"
        panel_dir.mkdir(parents=True)
        (panel_dir / "session.txt").write_text("keep", encoding="utf-8")

        changed = refresh_main_webview_cache_if_needed(data_dir, "1.4.2")

        assert changed is True
        assert not volatile_dir.exists()
        assert local_storage_dir.exists()
        assert panel_dir.exists()

        marker = json.loads((data_dir / "webview_ui_version.json").read_text(encoding="utf-8"))
        assert marker["version"] == "1.4.2"
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_refresh_main_webview_cache_skips_when_version_unchanged():
    data_dir = _make_test_dir("test_webview_cache_skip")
    try:
        volatile_dir = data_dir / "webview_storage" / "EBWebView" / "Default" / "Cache"
        volatile_dir.mkdir(parents=True)
        (volatile_dir / "cached.js").write_text("old", encoding="utf-8")

        (data_dir / "webview_ui_version.json").write_text(
            json.dumps({"version": "1.4.2"}),
            encoding="utf-8",
        )

        changed = refresh_main_webview_cache_if_needed(data_dir, "1.4.2")

        assert changed is False
        assert volatile_dir.exists()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
