"""Selective cache refresh for the embedded main webview UI."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)

_VERSION_MARKER = "webview_ui_version.json"
_VOLATILE_CACHE_PATHS = (
    Path("webview_storage") / "EBWebView" / "Default" / "Cache",
    Path("webview_storage") / "EBWebView" / "Default" / "Code Cache",
    Path("webview_storage") / "EBWebView" / "Default" / "GPUCache",
    Path("webview_storage") / "EBWebView" / "Default" / "DawnGraphiteCache",
    Path("webview_storage") / "EBWebView" / "Default" / "DawnWebGPUCache",
    Path("webview_storage") / "EBWebView" / "Default" / "Service Worker" / "CacheStorage",
    Path("webview_storage") / "EBWebView" / "Default" / "Service Worker" / "ScriptCache",
    Path("webview_storage") / "EBWebView" / "GraphiteDawnCache",
    Path("webview_storage") / "EBWebView" / "GrShaderCache",
    Path("webview_storage") / "EBWebView" / "ShaderCache",
)


def _load_cached_version(marker_path: Path) -> str:
    if not marker_path.exists():
        return ""
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        value = data.get("version", "")
        return value.strip() if isinstance(value, str) else ""
    except Exception:
        return ""


def _save_cached_version(marker_path: Path, version: str) -> None:
    marker_path.write_text(
        json.dumps({"version": version}, indent=2),
        encoding="utf-8",
    )


def _is_within(base_dir: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base_dir.resolve())
        return True
    except Exception:
        return False


def refresh_main_webview_cache_if_needed(data_dir: Path, current_version: str) -> bool:
    """Clear volatile main-webview caches once per app version.

    This preserves the Fibery side panel login because that panel uses its own
    `fibery_panel` user-data folder, separate from the main UI cache paths
    listed here.
    """
    marker_path = data_dir / _VERSION_MARKER
    previous_version = _load_cached_version(marker_path)
    normalized_version = (current_version or "").strip()
    if previous_version == normalized_version:
        return False

    cleared_paths: list[str] = []
    for relative_path in _VOLATILE_CACHE_PATHS:
        target = data_dir / relative_path
        if not target.exists() or not _is_within(data_dir, target):
            continue
        try:
            shutil.rmtree(target)
            cleared_paths.append(str(relative_path))
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Failed to clear webview cache path %s: %s", target, exc)

    _save_cached_version(marker_path, normalized_version)
    logger.info(
        "Webview cache refresh check complete (previous=%s, current=%s, cleared=%d)",
        previous_version or "<none>",
        normalized_version or "<unknown>",
        len(cleared_paths),
    )
    return True
