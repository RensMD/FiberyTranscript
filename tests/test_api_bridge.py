from types import SimpleNamespace

from ui.api_bridge import ApiBridge


def test_get_file_dialog_type_prefers_modern_pywebview_enum():
    webview_module = SimpleNamespace(
        FileDialog=SimpleNamespace(OPEN="modern-open", FOLDER="modern-folder"),
        OPEN_DIALOG="legacy-open",
        FOLDER_DIALOG="legacy-folder",
    )

    assert ApiBridge._get_file_dialog_type(webview_module, "open") == "modern-open"
    assert ApiBridge._get_file_dialog_type(webview_module, "folder") == "modern-folder"


def test_get_file_dialog_type_falls_back_to_legacy_constants():
    webview_module = SimpleNamespace(
        OPEN_DIALOG="legacy-open",
        FOLDER_DIALOG="legacy-folder",
    )

    assert ApiBridge._get_file_dialog_type(webview_module, "open") == "legacy-open"
    assert ApiBridge._get_file_dialog_type(webview_module, "folder") == "legacy-folder"
