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
    assert "applySummarizeButtonState(hasTranscript);" in summary_state_block
    assert "copyTranscriptBtn.disabled = !hasTranscript || finalizeInProgress;" in summary_state_block
    assert "copySummaryBtn.disabled = !generatedSummary || finalizeInProgress;" in summary_state_block

    transcribe_start = app_js.index("transcribeBtn.addEventListener('click', async () => {")
    transcribe_end = app_js.index("function formatAudioFileInfo(info) {")
    transcribe_block = app_js[transcribe_start:transcribe_end]
    assert "hasCompletedTranscription = false;" in transcribe_block
    assert "preparedAudio = result.prepared_audio;" in transcribe_block
    assert "selectedUploadPath = result.prepared_audio.file_path;" in transcribe_block

    upload_success_start = app_js.index("window.onAudioUploadedToFibery = function() {")
    upload_success_end = app_js.index("window.onAudioUploadError = function(message) {")
    upload_success_block = app_js[upload_success_start:upload_success_end]
    assert "updateSummaryActionsState();" in upload_success_block

    upload_error_end = app_js.index("// === Audio Health ===")
    upload_error_block = app_js[upload_success_end:upload_error_end]
    assert "updateSummaryActionsState();" in upload_error_block


def test_transcribe_controls_layout_is_inline_and_uses_context_toggle():
    index_html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    settings_js = (PROJECT_ROOT / "ui" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    styles_css = (PROJECT_ROOT / "ui" / "static" / "css" / "styles.css").read_text(encoding="utf-8")

    assert 'class="option-row"' in index_html
    assert 'id="recordingModeMicOnly"' in index_html
    assert 'id="recordingModeMicAndSpeakers"' in index_html
    assert 'id="improveTranscriptContextNo"' in index_html
    assert 'id="improveTranscriptContextYes"' in index_html
    assert "option-row" in styles_css
    assert "IMPROVE_TRANSCRIPT_WITH_CONTEXT = true" not in app_js
    assert "function getSelectedTranscriptContextImprovement()" in app_js
    assert "function applyTranscriptContextImprovementDefault(database = currentEntityDb)" in app_js
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
    assert "getSelectedTranscriptContextImprovement()," in app_js

    recording_toggle_index = index_html.index('id="recordingModeMicOnly"')
    context_toggle_index = index_html.index('id="improveTranscriptContextNo"')
    transcript_toggle_index = index_html.index('id="modeAppend"')
    transcribe_btn_index = index_html.index('id="transcribeBtn"')
    assert recording_toggle_index < context_toggle_index
    assert context_toggle_index < transcript_toggle_index
    assert recording_toggle_index < transcript_toggle_index
    assert transcript_toggle_index < transcribe_btn_index

    transcribe_start = app_js.index("transcribeBtn.addEventListener('click', async () => {")
    retry_start = app_js.index("retryBatchBtn.addEventListener('click', async () => {")
    retry_end = app_js.index("// === Transcript Actions ===")
    transcribe_block = app_js[transcribe_start:retry_start]
    retry_block = app_js[retry_start:retry_end]
    assert "getSelectedTranscriptContextImprovement()," in transcribe_block
    assert "getSelectedTranscriptContextImprovement()," in retry_block


def test_summary_language_toggle_defaults_to_english_and_is_session_scoped():
    index_html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="summaryLanguageEnglish"' in index_html
    assert 'id="summaryLanguageDutch"' in index_html
    assert 'name="summaryLanguage" value="en" checked' in index_html
    assert "function getSelectedSummaryLanguage()" in app_js
    assert "window.pywebview.api.set_summary_language(value);" in app_js
    assert "syncSummaryLanguageInputs('en');" in app_js


def test_header_tabs_and_recording_handoff_contract_exists():
    index_html = (PROJECT_ROOT / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    bridge_py = (PROJECT_ROOT / "ui" / "api_bridge.py").read_text(encoding="utf-8")

    assert 'id="mainTab"' in index_html
    assert 'id="recordingTab"' in index_html
    assert 'id="mainTabPanel"' in index_html
    assert 'id="recordingTabPanel"' in index_html
    assert 'recordingPanel' in index_html
    assert 'id="goToRecordingBtn"' in index_html
    assert 'id="stagedAudioInfo"' in index_html
    assert 'id="stagedAudioName"' in index_html
    assert 'id="stagedAudioMeta"' in index_html
    assert 'id="clearStagedAudioBtn"' in index_html
    assert "finalizeRecordingToMain(" in app_js
    assert "handleFinalizeTimeout(" in app_js
    assert "reconcileUiWithBackendState(" in app_js
    assert "beginRecordingFinalizeToMain(" in app_js
    assert "reset_session_keep_meeting" in bridge_py

    new_meeting_btn_index = index_html.index('id="newMeetingBtn"')
    meeting_panel_index = index_html.index('id="fiberyLinkPanel"')
    assert new_meeting_btn_index < meeting_panel_index


def test_staged_audio_clear_button_is_available_for_recordings_before_transcription():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "clearUploadBtn" not in app_js
    assert "clearStagedAudioBtn.classList.remove('hidden');" in app_js


def test_recording_finalize_lock_guards_exist_for_tabs_and_timeouts():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    sync_start = app_js.index("function syncTabLockState() {")
    sync_end = app_js.index("function setWorkflowControlsDisabled(")
    sync_block = app_js[sync_start:sync_end]
    assert "mainTab.disabled = isRecording || finalizeInProgress;" in sync_block
    assert "recordingTab.disabled = finalizeInProgress;" in sync_block
    assert "setActiveTab('recording', { force: true });" in sync_block
    assert "setActiveTab('main', { force: true });" in sync_block

    timeout_start = app_js.index("async function handleFinalizeTimeout() {")
    timeout_end = app_js.index("mainTab.addEventListener('click', async () => {")
    timeout_block = app_js[timeout_start:timeout_end]
    assert "snapshot.state === 'recording'" in timeout_block
    assert "snapshot.state === 'prepared' || snapshot.state === 'completed'" in timeout_block
    assert "refreshFinalizeStatus()" in timeout_block
    assert "finalizePollHandle = setInterval" in timeout_block


def test_summarize_button_shows_elapsed_progress_during_long_requests():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "function formatSummarizeLabel()" in app_js
    assert "function startSummarizeProgressTimer()" in app_js
    assert "function stopSummarizeProgressTimer()" in app_js
    assert "Summarizing (${mins}m ${secs}s)..." in app_js
    assert "startSummarizeProgressTimer();" in app_js
    assert app_js.count("stopSummarizeProgressTimer();") >= 3


def test_problem_generation_enablement_rechecks_fibery_after_summary_writes():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "async function refreshProblemsReadyState(" in app_js
    assert "const result = await callApi('check_problems_ready');" in app_js

    summary_start = app_js.index("window.onSummarizeComplete = function(result) {")
    summary_end = app_js.index("// === Pending summary sent after link was added ===")
    summary_block = app_js[summary_start:summary_end]
    assert "refreshProblemsReadyState();" in summary_block

    pending_start = app_js.index("window.onPendingSummarySent = function() {")
    pending_end = app_js.index("window.onPendingSummarySendError = function(message) {")
    pending_block = app_js[pending_start:pending_end]
    assert "refreshProblemsReadyState();" in pending_block


def test_recording_tab_owns_idle_audio_preview_lifecycle():
    app_js = (PROJECT_ROOT / "ui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    bridge_py = (PROJECT_ROOT / "ui" / "api_bridge.py").read_text(encoding="utf-8")

    assert "function reconcileAudioPreviewState(" in app_js
    assert "function scheduleAudioPreviewReconcile(" in app_js
    assert "await callApi('stop_monitor');" in app_js
    assert "await startMonitoring({ includeLoopback: true });" in app_js
    assert "setAudioSourceToolsHidden(false);" in app_js
    assert "await loadDevices();" not in app_js

    set_tab_start = app_js.index("function setActiveTab(tab, { force = false } = {}) {")
    set_tab_end = app_js.index("function shouldRunRecordingTabPreview(")
    set_tab_block = app_js[set_tab_start:set_tab_end]
    assert "scheduleAudioPreviewReconcile({" in set_tab_block
    assert "refreshDevices: activeTab === 'recording'" in set_tab_block

    init_start = app_js.index("async function initApp() {")
    init_end = app_js.index("// === API Key Setup ===")
    init_block = app_js[init_start:init_end]
    assert "await autoDetectDevicesOnce();" not in init_block
    assert "await loadDevices();" not in init_block

    refresh_start = app_js.index("refreshDevicesBtn.addEventListener('click', async () => {")
    refresh_end = app_js.index("// === Step 1: Fibery Meeting Selection ===")
    refresh_block = app_js[refresh_start:refresh_end]
    assert "await reconcileAudioPreviewState({ refreshDevices: true, autoSelectActive: true });" in refresh_block

    select_start = app_js.index("async function selectMeetingFromPanel() {")
    select_end = app_js.index("// === Create Meeting ===")
    select_block = app_js[select_start:select_end]
    assert "await autoDetectDevicesOnce();" not in select_block

    create_start = app_js.index("async function createMeeting(meetingType) {")
    create_end = app_js.index("function resetFiberyValidation() {")
    create_block = app_js[create_start:create_end]
    assert "await autoDetectDevicesOnce();" not in create_block

    assert "include_loopback: bool = False" in bridge_py
