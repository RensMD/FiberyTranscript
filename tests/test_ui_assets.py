from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_summary_status_copy_button_exists_in_index():
    index_html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="copySummaryStatusBtn"' in index_html
    assert "copySummaryStatusBtn" in app_js
