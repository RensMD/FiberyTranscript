import sys
import types

from config.constants import APP_AUTOSTART_REG_VALUE, APP_LEGACY_AUTOSTART_REG_VALUES
from utils import autostart


def _make_fake_winreg(initial_values=None):
    state = {"values": dict(initial_values or {})}
    module = types.ModuleType("winreg")
    module.HKEY_CURRENT_USER = object()
    module.KEY_SET_VALUE = object()
    module.REG_SZ = object()

    def open_key(root, path, reserved, access):
        return "RUN-KEY"

    def set_value_ex(key, name, reserved, kind, value):
        state["values"][name] = value

    def delete_value(key, name):
        if name not in state["values"]:
            raise FileNotFoundError(name)
        del state["values"][name]

    def close_key(key):
        return None

    module.OpenKey = open_key
    module.SetValueEx = set_value_ex
    module.DeleteValue = delete_value
    module.CloseKey = close_key
    module._state = state
    return module


def test_autostart_windows_enable_rewrites_legacy_value(monkeypatch):
    fake_winreg = _make_fake_winreg({"Fibery Transcript": '"C:\\Old\\FiberyTranscript.exe"'})

    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(autostart.sys, "executable", r"C:\New\FiberyTranscript.exe")

    assert autostart._autostart_windows(True) is True
    assert fake_winreg._state["values"][APP_AUTOSTART_REG_VALUE] == '"C:\\New\\FiberyTranscript.exe"'
    for legacy_name in APP_LEGACY_AUTOSTART_REG_VALUES:
        assert legacy_name not in fake_winreg._state["values"]


def test_autostart_windows_disable_removes_all_known_value_names(monkeypatch):
    initial_values = {
        APP_AUTOSTART_REG_VALUE: '"C:\\New\\FiberyTranscript.exe"',
        "Fibery Transcript": '"C:\\Old\\FiberyTranscript.exe"',
    }
    fake_winreg = _make_fake_winreg(initial_values)

    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert autostart._autostart_windows(False) is True
    assert fake_winreg._state["values"] == {}
