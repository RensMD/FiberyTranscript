from pathlib import Path

from config.constants import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_summary_status_row_is_error_only_and_supports_resummarize_label():
    index_html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="summaryStatusRow"' in index_html
    assert 'id="copySummaryStatusBtn"' not in index_html
    assert "copySummaryStatusBtn" not in app_js
    assert "Resummarize" in app_js
    assert "type === 'error' || type === 'warning'" in app_js
    for asset in (
        "icon.ico",
        "icon.png",
        "icon.svg",
        "css/styles.css",
        "js/audio-viz.js",
        "js/transcript.js",
        "js/settings.js",
        "js/app.js",
    ):
        assert f'{asset}?v={APP_VERSION}' in index_html


def test_summary_actions_remain_available_during_post_transcription_upload():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    summary_state_start = app_js.index("function updateSummaryActionsState(scrollIntoView = false) {")
    summary_state_end = app_js.index("function applySummarizeButtonState(")
    summary_state_block = app_js[summary_state_start:summary_state_end]
    assert "const summaryBlockedByCoreTranscription = transcriptionInProgress && !hasCompletedTranscription;" in summary_state_block
    assert "(fiberyValidated || hasTranscript);" in summary_state_block

    transcribe_start = app_js.index("transcribeBtn.addEventListener('click', async () => {")
    transcribe_end = app_js.index("function formatAudioFileInfo(info) {")
    transcribe_block = app_js[transcribe_start:transcribe_end]
    assert "hasCompletedTranscription = false;" in transcribe_block

    upload_success_start = app_js.index("window.onAudioUploadedToFibery = function() {")
    upload_success_end = app_js.index("window.onAudioUploadError = function(message) {")
    upload_success_block = app_js[upload_success_start:upload_success_end]
    assert "updateSummaryActionsState();" in upload_success_block

    upload_error_end = app_js.index("// === Audio Health ===")
    upload_error_block = app_js[upload_success_end:upload_error_end]
    assert "updateSummaryActionsState();" in upload_error_block


def test_transcribe_controls_layout_is_inline_and_always_uses_context_improvement():
    index_html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    settings_js = (PROJECT_ROOT / "ui" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    styles_css = (PROJECT_ROOT / "ui" / "static" / "css" / "styles.css").read_text(encoding="utf-8")

    assert 'class="transcript-mode-controls"' in index_html
    assert 'id="recordingModeMicOnly"' in index_html
    assert 'id="recordingModeMicAndSpeakers"' in index_html
    assert "transcript-mode-controls" in styles_css
    assert "IMPROVE_TRANSCRIPT_WITH_CONTEXT = true" in app_js
    assert "advancedTranscriptCard" not in index_html
    assert "advancedTranscriptCard" not in app_js
    assert "advanced-transcript-card" not in styles_css
    assert 'id="improveTranscriptCheckbox"' not in index_html
    assert "improveTranscriptCheckbox" not in app_js
    assert "File Ready" not in app_js
    assert 'id="removeEchoCheckbox"' not in index_html
    assert "removeEchoCheckbox" not in app_js
    assert 'id="echoCancellationEnabled"' in index_html
    assert "echoCancellationEnabled" in settings_js
    assert "function getSelectedRecordingMode()" in app_js
    assert "window.pywebview.api.set_recording_mode(value);" in app_js

    recording_toggle_index = index_html.index('id="recordingModeMicOnly"')
    transcript_toggle_index = index_html.index('id="modeAppend"')
    transcribe_btn_index = index_html.index('id="transcribeBtn"')
    assert recording_toggle_index < transcript_toggle_index
    assert transcript_toggle_index < transcribe_btn_index


def test_summary_language_toggle_defaults_to_english_and_is_session_scoped():
    index_html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="summaryLanguageEnglish"' in index_html
    assert 'id="summaryLanguageDutch"' in index_html
    assert 'name="summaryLanguage" value="en" checked' in index_html
    assert "function getSelectedSummaryLanguage()" in app_js
    assert "window.pywebview.api.set_summary_language(value);" in app_js
    assert "syncSummaryLanguageInputs('en');" in app_js


def test_clear_button_is_available_for_staged_recordings_before_transcription():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "clearUploadBtn.classList.toggle('hidden', !info.is_uploaded_file);" not in app_js
    assert "clearUploadBtn.classList.remove('hidden');" in app_js


def test_summarize_button_shows_elapsed_progress_during_long_requests():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "function formatSummarizeLabel()" in app_js
    assert "function startSummarizeProgressTimer()" in app_js
    assert "function stopSummarizeProgressTimer()" in app_js
    assert "Summarizing (${mins}m ${secs}s)..." in app_js
    assert "startSummarizeProgressTimer();" in app_js
    assert app_js.count("stopSummarizeProgressTimer();") >= 3
