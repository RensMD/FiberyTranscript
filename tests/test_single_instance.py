from config.constants import APP_WINDOW_TITLE
from utils import single_instance


def test_acquire_single_instance_guard_focuses_existing_window(monkeypatch):
    focused_titles = []

    monkeypatch.setattr(single_instance.sys, "platform", "win32")
    monkeypatch.setattr(
        single_instance,
        "_create_windows_mutex",
        lambda name: single_instance._WINDOWS_MUTEX_ALREADY_EXISTS,
    )
    monkeypatch.setattr(
        single_instance,
        "_focus_existing_window",
        lambda title: focused_titles.append(title) or True,
    )

    guard = single_instance.acquire_single_instance_guard()

    assert guard is None
    assert focused_titles == [APP_WINDOW_TITLE]


def test_acquire_single_instance_guard_releases_handle_once(monkeypatch):
    released_handles = []

    monkeypatch.setattr(single_instance.sys, "platform", "win32")
    monkeypatch.setattr(single_instance, "_create_windows_mutex", lambda name: "HANDLE-1")
    monkeypatch.setattr(
        single_instance,
        "_close_windows_handle",
        lambda handle: released_handles.append(handle),
    )

    guard = single_instance.acquire_single_instance_guard()
    assert guard is not None

    guard.release()
    guard.release()

    assert released_handles == ["HANDLE-1"]
