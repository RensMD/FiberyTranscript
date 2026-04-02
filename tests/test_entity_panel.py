from unittest.mock import MagicMock

import ui.entity_panel as entity_panel_module
from ui.entity_panel import EntityPanel


def test_notify_url_change_uses_injected_js_notifier(monkeypatch):
    notify_js = MagicMock()
    panel = EntityPanel(main_window=None, notify_js=notify_js)
    started = []

    class _ImmediateThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self.daemon = daemon
            started.append(self)

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(entity_panel_module.threading, "Thread", _ImmediateThread)

    panel._notify_url_change("https://example.fibery.io/General/Internal_Meeting/test-123")

    assert len(started) == 1
    assert started[0].daemon is True
    notify_js.assert_called_once()
    js_code = notify_js.call_args.args[0]
    assert "window.onPanelUrlChanged" in js_code
    assert '"https://example.fibery.io/General/Internal_Meeting/test-123"' in js_code


def test_notify_url_change_ignores_empty_urls():
    notify_js = MagicMock()
    panel = EntityPanel(main_window=None, notify_js=notify_js)

    panel._notify_url_change("")

    notify_js.assert_not_called()
