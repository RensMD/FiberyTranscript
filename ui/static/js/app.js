/**
 * Main application logic.
 * Flow: validate Fibery link → record → review & send (with instructions).
 */

// --- Toast Notifications ---
function showToast(message, type = 'info', duration = 5000) {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('toast-out');
        toast.addEventListener('animationend', () => toast.remove());
    }, duration);
}

// --- API Helper (for methods that return {success: bool}) ---
async function callApi(method, ...args) {
    const result = await window.pywebview.api[method](...args);
    if (result && typeof result === 'object' && result.success === false) {
        throw new Error(result.error || 'Unknown error');
    }
    return result;
}

// --- State ---
let isRecording = false;
let timerInterval = null;
let startTime = null;
let timerAccumulatedMs = 0;
let fiberyValidated = false;      // true once the link has been validated
let currentFiberyUrl = '';        // the validated URL
let generatedSummary = '';        // cached summary text from last successful summarize
let linkedTranscriptText = '';    // transcript pulled from the linked Fibery meeting
let selectedUploadPath = null;    // path to the currently staged audio file
let preparedAudio = null;         // staged recording/upload waiting for transcription
let currentEntityDb = '';         // entity database name (for Files support check)
let summarizeInProgress = false;  // true while Gemini summary is running
let summarizeRetryPending = false;
let summarizeStartedAt = 0;
let summarizeProgressTimer = null;
let hasCompletedSummary = false;
let transcriptionInProgress = false;
let hasCompletedTranscription = false;
const IMPROVE_TRANSCRIPT_WITH_CONTEXT = true;

// --- DOM elements ---
const recordBtn = document.getElementById('recordBtn');
const recordBtnText = document.getElementById('recordBtnText');
const recordTimer = document.getElementById('recordTimer');
const audioSourceTools = document.getElementById('audioSourceTools');
const micSelect = document.getElementById('micSelect');
const loopbackSelect = document.getElementById('loopbackSelect');
const refreshDevicesBtn = document.getElementById('refreshDevicesBtn');

// Step 1 – Fibery meeting selection
const selectMeetingBtn = document.getElementById('selectMeetingBtn');
const fiberySelectRow = document.getElementById('fiberySelectRow');
const fiberySelectHint = document.getElementById('fiberySelectHint');
const fiberyEntityInfo = document.getElementById('fiberyEntityInfo');
const entityName = document.getElementById('entityName');
const entityDb = document.getElementById('entityDb');
const changeLinkBtn = document.getElementById('changeLinkBtn');
const fiberyValidateStatus = document.getElementById('fiberyValidateStatus');
const fiberyMissingWarning = document.getElementById('fiberyMissingWarning');
const fiberyDisambiguation = document.getElementById('fiberyDisambiguation');
const disambigOptions = document.getElementById('disambigOptions');
const createMeetingDividerRow = document.getElementById('createMeetingDividerRow');
const createMeetingFields = document.getElementById('createMeetingFields');
const createMeetingName = document.getElementById('createMeetingName');
const entityLink = document.getElementById('entityLink');
let currentEntityUrl = '';  // URL for the validated/created entity
let panelCurrentUrl = '';   // Current URL in the Fibery panel

// Collapsible elements
const audioStorageCollapsible = document.getElementById('audioStorageCollapsible');
const recordingMetaCollapsible = document.getElementById('recordingMetaCollapsible');
const uploadCollapsible = document.getElementById('uploadCollapsible');
const transcribePanelCollapsible = document.getElementById('transcribePanelCollapsible');
const sendPanelCollapsible = document.getElementById('sendPanelCollapsible');
const newMeetingBtn = document.getElementById('newMeetingBtn');
const retryBatchBtn = document.getElementById('retryBatchBtn');

// Open entity link in the in-app Fibery panel
document.getElementById('entityLink').addEventListener('click', (e) => {
    e.preventDefault();
    if (currentEntityUrl) {
        window.pywebview.api.navigate_entity_panel(currentEntityUrl);
    }
});

// Step 2 – Upload controls
const browseAudioBtn = document.getElementById('browseAudioBtn');
const uploadFileInfoEl = document.getElementById('uploadFileInfo');
const uploadFileName = document.getElementById('uploadFileName');
const uploadFileMeta = document.getElementById('uploadFileMeta');
const clearUploadBtn = document.getElementById('clearUploadBtn');
const uploadDivider = document.getElementById('uploadDivider');
const uploadControls = document.getElementById('uploadControls');
const audioStorageHint = document.getElementById('audioStorageHint');

// Step 3 - Transcribe
const transcribeBtn = document.getElementById('transcribeBtn');

// Step 3 – AI summary
const additionalPrompt = document.getElementById('additionalPrompt');
const sendActions = document.getElementById('sendActions');
const summarizeBtn = document.getElementById('summarizeBtn');
const summaryStatusRow = document.getElementById('summaryStatusRow');
const summaryStatusBadge = document.getElementById('summaryStatusBadge');
const copyTranscriptBtn = document.getElementById('copyTranscriptBtn');
const copySummaryBtn = document.getElementById('copySummaryBtn');
const retryRow = document.getElementById('retryRow');
const retryTranscriptBtn = document.getElementById('retryTranscriptBtn');
const retryAudioUploadBtn = document.getElementById('retryAudioUploadBtn');

// === Initialization ===
window.addEventListener('pywebviewready', async () => {
    await loadSettings();

    // Check if API keys are configured
    const keysStatus = await window.pywebview.api.get_api_keys_status();
    if (!keysStatus.configured) {
        showSetupOverlay();
        return; // Don't init audio until keys are set up
    }

    await initApp();
});

async function initApp() {
    await loadDevices();
    await autoDetectDevicesOnce();

    // Open entity panel with default workspace URL from backend config
    window.pywebview.api.open_entity_panel();
}

// === API Key Setup ===
function showSetupOverlay() {
    document.getElementById('setupOverlay').classList.add('open');
}

function hideSetupOverlay() {
    document.getElementById('setupOverlay').classList.remove('open');
}

document.getElementById('setupSaveBtn').addEventListener('click', async () => {
    const assemblyai = document.getElementById('setupAssemblyAI').value.trim();
    const gemini = document.getElementById('setupGemini').value.trim();
    const fibery = document.getElementById('setupFibery').value.trim();
    const statusEl = document.getElementById('setupStatus');

    if (!assemblyai || !gemini || !fibery) {
        statusEl.textContent = 'All three keys are required.';
        statusEl.className = 'setup-status error';
        return;
    }

    statusEl.textContent = 'Saving...';
    statusEl.className = 'setup-status';

    try {
        const result = await window.pywebview.api.save_api_keys({
            assemblyai_api_key: assemblyai,
            gemini_api_key: gemini,
            fibery_api_token: fibery,
        });
        if (result.success) {
            statusEl.textContent = 'Keys saved!';
            statusEl.className = 'setup-status success';
            setTimeout(async () => {
                hideSetupOverlay();
                await initApp();
            }, 500);
        } else {
            statusEl.textContent = 'Failed to save keys: ' + (result.error || 'Unknown error');
            statusEl.className = 'setup-status error';
        }
    } catch (err) {
        statusEl.textContent = 'Error: ' + err;
        statusEl.className = 'setup-status error';
    }
});

function populateDeviceOptions(devices) {
    micSelect.innerHTML = '<option value="">-- Select Microphone --</option>';
    for (const dev of devices.microphones) {
        const opt = document.createElement('option');
        opt.value = dev.index;
        opt.textContent = dev.name;
        micSelect.appendChild(opt);
    }

    loopbackSelect.innerHTML = '<option value="">-- Select Speaker Output --</option>';
    for (const dev of devices.loopbacks) {
        const opt = document.createElement('option');
        opt.value = dev.index;
        opt.textContent = dev.name;
        loopbackSelect.appendChild(opt);
    }

    if (devices.microphones.length > 0) micSelect.value = devices.microphones[0].index;
    if (devices.loopbacks.length > 0) loopbackSelect.value = devices.loopbacks[0].index;
}

async function loadDevices() {
    try {
        const devices = await window.pywebview.api.get_audio_devices();
        populateDeviceOptions(devices);
    } catch (err) {
        console.error('Failed to load devices:', err);
    }
}

async function refreshDeviceList({ reinitializeBackends = false, preserveSelection = true } = {}) {
    if (isRecording || transcriptionInProgress) {
        return false;
    }

    const prevMic = micSelect.value;
    const prevLoop = loopbackSelect.value;

    try {
        const devices = reinitializeBackends
            ? await window.pywebview.api.refresh_audio_devices()
            : await window.pywebview.api.get_audio_devices();
        if (devices.error || (devices.microphones.length === 0 && devices.loopbacks.length === 0)) {
            console.warn('Device refresh returned error or empty, keeping current list');
            return false;
        }

        populateDeviceOptions(devices);
    } catch (err) {
        console.warn('Device refresh failed, keeping current list:', err);
        return false;
    }

    if (preserveSelection) {
        if (prevMic && micSelect.querySelector(`option[value="${prevMic}"]`)) {
            micSelect.value = prevMic;
        }
        if (prevLoop && loopbackSelect.querySelector(`option[value="${prevLoop}"]`)) {
            loopbackSelect.value = prevLoop;
        }
    }

    return true;
}

async function autoSelectDevices() {
    try {
        const scanResults = await window.pywebview.api.scan_devices();

        // Auto-select the microphone with the highest peak_rms (if any are active)
        const activeMics = scanResults.microphones
            .filter(r => r.is_active && !r.scan_failed)
            .sort((a, b) => b.peak_rms - a.peak_rms);

        if (activeMics.length > 0) {
            const best = activeMics[0];
            if (micSelect.querySelector(`option[value="${best.device_index}"]`)) {
                micSelect.value = best.device_index;
                console.log(`Auto-selected mic: ${best.device_name} (RMS: ${best.peak_rms})`);
            }
        }

        // Auto-select the loopback with the highest peak_rms (if any are active)
        const activeLoopbacks = scanResults.loopbacks
            .filter(r => r.is_active && !r.scan_failed)
            .sort((a, b) => b.peak_rms - a.peak_rms);

        if (activeLoopbacks.length > 0) {
            const best = activeLoopbacks[0];
            if (loopbackSelect.querySelector(`option[value="${best.device_index}"]`)) {
                loopbackSelect.value = best.device_index;
                console.log(`Auto-selected loopback: ${best.device_name} (RMS: ${best.peak_rms})`);
            }
        }
    } catch (err) {
        console.warn('Device auto-detection failed, using defaults:', err);
    }
}

async function autoDetectDevicesOnce({ refreshDevices = false } = {}) {
    if (isRecording || transcriptionInProgress) return;

    if (refreshDevices) {
        await refreshDeviceList({ reinitializeBackends: true, preserveSelection: true });
    }

    await autoSelectDevices();
    await startMonitoring();
}

async function loadSettings() {
    try {
        const settings = await window.pywebview.api.get_settings();
        document.body.setAttribute('data-theme', settings.theme || 'dark');

        // Cache the default audio storage from settings
        window._defaultAudioStorage = settings.audio_storage || 'local';

        // Initialize audio storage radio from settings
        const storageValue = settings.audio_storage || 'local';
        const storageRadio = document.querySelector(`input[name="audioStorage"][value="${storageValue}"]`);
        if (storageRadio) storageRadio.checked = true;
        updateAudioStorageState();
    } catch (err) {
        console.error('Failed to load settings:', err);
    }
}

// === Audio Storage Toggle ===
function updateAudioStorageState() {
    const fiberyRadio = document.getElementById('storageFibery');
    const localRadio = document.getElementById('storageLocal');

    if (!fiberyValidated) {
        fiberyRadio.disabled = true;
        audioStorageHint.textContent = 'Link a meeting first';
        if (fiberyRadio.checked) localRadio.checked = true;
    } else {
        const wasDisabled = fiberyRadio.disabled;
        fiberyRadio.disabled = false;
        audioStorageHint.textContent = '';
        // Restore the user's default when Fibery first becomes available
        // (it was forced to "local" while no meeting was linked)
        if (wasDisabled) {
            const defaultStorage = window._defaultAudioStorage || 'local';
            const radio = document.querySelector(`input[name="audioStorage"][value="${defaultStorage}"]`);
            if (radio) radio.checked = true;
        }
    }
}

document.querySelectorAll('input[name="audioStorage"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        window.pywebview.api.save_settings({ audio_storage: e.target.value });
    });
});

function setAudioSourceToolsHidden(hidden) {
    audioSourceTools.classList.toggle('hidden', hidden);
    refreshDevicesBtn.classList.toggle('hidden', hidden);
}

function hasPreparedAudio() {
    return Boolean(preparedAudio && preparedAudio.file_path);
}

function getSelectedTranscriptMode() {
    const selected = document.querySelector('input[name="transcriptMode"]:checked');
    return selected ? selected.value : 'append';
}

function getSelectedRecordingMode() {
    const selected = document.querySelector('input[name="recordingMode"]:checked');
    return selected ? selected.value : 'mic_only';
}

function syncRecordingModeInputs(value) {
    const normalized = value === 'mic_and_speakers' ? 'mic_and_speakers' : 'mic_only';
    const radio = document.getElementById(
        normalized === 'mic_and_speakers' ? 'recordingModeMicAndSpeakers' : 'recordingModeMicOnly'
    );
    if (radio) radio.checked = true;
}

function applyRecordingMode(value) {
    syncRecordingModeInputs(value);
    window.pywebview.api.set_recording_mode(value);
}

function syncTranscriptModeInputs(value) {
    const normalized = value === 'replace' ? 'replace' : 'append';
    const mainRadio = document.getElementById(normalized === 'append' ? 'modeAppend' : 'modeReplace');
    if (mainRadio) mainRadio.checked = true;
}

function applyTranscriptMode(value) {
    syncTranscriptModeInputs(value);
    window.pywebview.api.set_transcript_mode(value);
}

function getSelectedSummaryLanguage() {
    const selected = document.querySelector('input[name="summaryLanguage"]:checked');
    return selected ? selected.value : 'en';
}

function syncSummaryLanguageInputs(value) {
    const normalized = value === 'nl' ? 'nl' : 'en';
    const radio = document.getElementById(
        normalized === 'nl' ? 'summaryLanguageDutch' : 'summaryLanguageEnglish'
    );
    if (radio) radio.checked = true;
}

function applySummaryLanguage(value) {
    syncSummaryLanguageInputs(value);
    window.pywebview.api.set_summary_language(value);
}

function setSelectedUploadUiVisible(visible) {
    uploadFileInfoEl.classList.toggle('hidden', !visible);
    audioStorageCollapsible.classList.toggle('collapsed', !visible);
    uploadDivider.classList.toggle('hidden', visible);
}

function updateTranscribeButton(progressText = '') {
    transcribeBtn.classList.toggle('processing', transcriptionInProgress);
    transcribeBtn.disabled = !hasPreparedAudio() || transcriptionInProgress;
    if (transcriptionInProgress) {
        transcribeBtn.textContent = progressText || 'Transcribing...';
        return;
    }
    transcribeBtn.textContent = hasCompletedTranscription ? 'Retranscribe' : 'Transcribe';
}

function clearSummaryForRetranscribe() {
    generatedSummary = '';
    hasCompletedSummary = false;
    copySummaryBtn.disabled = true;
    summarizeRetryPending = false;
    summarizeInProgress = false;
    retryTranscriptBtn.style.display = 'none';
    retryAudioUploadBtn.style.display = 'none';
    retryRow.classList.add('hidden');
    retryBatchBtn.style.display = 'none';
    _lastFailedWavPath = '';
    setFiberyStatus('', '');
}

function applyPreparedAudio(info) {
    if (!info || !info.file_path) return;

    preparedAudio = info;
    selectedUploadPath = info.file_path;
    hasCompletedTranscription = false;
    transcriptionInProgress = false;

    uploadFileName.textContent = info.file_name || info.file_path.replace(/\\/g, '/').split('/').pop();
    uploadFileMeta.textContent = formatAudioFileInfo(info);
    clearUploadBtn.classList.remove('hidden');
    clearUploadBtn.disabled = false;
    browseAudioBtn.classList.add('hidden');
    uploadCollapsible.classList.remove('collapsed');
    recordingMetaCollapsible.classList.add('collapsed');
    setSelectedUploadUiVisible(true);
    transcribePanelCollapsible.classList.remove('collapsed');
    sendPanelCollapsible.classList.remove('collapsed');
    setAudioSourceToolsHidden(true);
    syncRecordingModeInputs(info.recording_mode_recommendation || 'mic_only');
    updateTranscribeButton();
    setStatus('completed');
    updateSummaryActionsState();
}

function clearPreparedAudioUi() {
    preparedAudio = null;
    selectedUploadPath = null;
    hasCompletedTranscription = false;
    transcriptionInProgress = false;
    uploadFileName.textContent = '';
    uploadFileMeta.textContent = '';
    browseAudioBtn.classList.remove('hidden');
    clearUploadBtn.classList.add('hidden');
    setSelectedUploadUiVisible(false);
    transcribePanelCollapsible.classList.add('collapsed');
    updateTranscribeButton();
}

updateTranscribeButton();

// === File Upload (Browse & Transcribe) ===
browseAudioBtn.addEventListener('click', async () => {
    try {
        const result = await window.pywebview.api.browse_audio_file();
        if (!result.success) return;

        const filePath = result.path;

        // Validate the file
        browseAudioBtn.disabled = true;
        browseAudioBtn.querySelector('span').textContent = 'Checking...';

        const validation = await window.pywebview.api.validate_audio_file(filePath);

        browseAudioBtn.disabled = false;
        browseAudioBtn.querySelector('span').textContent = 'Browse Audio File';

        if (!validation.success) {
            showToast('Invalid audio file: ' + validation.error, 'error');
            return;
        }

        browseAudioBtn.disabled = true;
        browseAudioBtn.querySelector('span').textContent = 'Preparing...';

        const prepared = await window.pywebview.api.prepare_uploaded_audio(filePath);
        browseAudioBtn.disabled = false;
        browseAudioBtn.querySelector('span').textContent = 'Browse Audio File';

        if (!prepared.success) {
            showToast('Could not prepare audio: ' + prepared.error, 'error');
            return;
        }

        clearSummaryForRetranscribe();
        applyPreparedAudio(prepared.prepared_audio || validation);
    } catch (err) {
        showToast('Error: ' + err, 'error');
        browseAudioBtn.disabled = false;
        browseAudioBtn.querySelector('span').textContent = 'Browse Audio File';
    }
});

clearUploadBtn.addEventListener('click', async () => {
    try {
        await callApi('clear_prepared_audio');
    } catch (err) {
        showToast('Could not clear staged audio: ' + err, 'error');
        return;
    }
    clearPreparedAudioUi();
    setAudioSourceToolsHidden(false);
    setStatus('', '');
    startMonitoring();
    updateSummaryActionsState();
});

transcribeBtn.addEventListener('click', async () => {
    if (!hasPreparedAudio()) return;

    transcriptionInProgress = true;
    hasCompletedTranscription = false;
    updateTranscribeButton('Starting...');
    clearSummaryForRetranscribe();
    if (window.transcriptManager?.clear) {
        window.transcriptManager.clear();
    }
    linkedTranscriptText = fiberyValidated ? linkedTranscriptText : '';

    // Warn if no meeting selected
    if (!fiberyValidated) {
        fiberyMissingWarning.classList.remove('hidden');
    }

    try {
        const result = await window.pywebview.api.start_transcription(
            false,
            IMPROVE_TRANSCRIPT_WITH_CONTEXT,
            getSelectedTranscriptMode(),
            getSelectedRecordingMode(),
        );
        if (!result.success) {
            showToast('Failed: ' + result.error, 'error');
            transcriptionInProgress = false;
            updateTranscribeButton();
            return;
        }
        if (result.transcript_mode) {
            syncTranscriptModeInputs(result.transcript_mode);
        }
        if (result.effective_recording_mode) {
            syncRecordingModeInputs(result.effective_recording_mode);
        }
        if (result.recording_mode_auto_corrected) {
            const reason = result.recording_mode_reason ? ` ${result.recording_mode_reason}` : '';
            showToast(`Using Mic only for this transcript.${reason}`, 'info', 7000);
        }
        clearUploadBtn.disabled = true;
        clearUploadBtn.classList.add('hidden');
        updateSummaryActionsState();
    } catch (err) {
        showToast('Error: ' + err, 'error');
        transcriptionInProgress = false;
        updateTranscribeButton();
    }
});

function formatAudioFileInfo(info) {
    const duration = formatAudioDuration(info.duration_seconds);
    const size = formatFileSize(info.size_bytes);
    return `${info.format} \u2022 ${duration} \u2022 ${size}`;
}

function formatAudioDuration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function formatFileSize(bytes) {
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
    if (bytes >= 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
    if (bytes >= 1e3) return (bytes / 1e3).toFixed(1) + ' KB';
    return bytes + ' B';
}

// === Fibery Audio Upload Callbacks ===
window.onAudioUploadedToFibery = function() {
    transcriptionInProgress = false;
    updateTranscribeButton();
    updateSummaryActionsState();
    showToast('Audio recording uploaded to Fibery', 'success');
    retryAudioUploadBtn.style.display = 'none';
};

window.onAudioUploadError = function(message) {
    transcriptionInProgress = false;
    const isEntityDeleted = message && (message.includes('not found') || message.includes('Not found'));
    if (isEntityDeleted) {
        showToast('Meeting was deleted in Fibery. Select a new meeting and retry the upload.', 'error', 10000);
    } else {
        showToast('Audio upload to Fibery failed: ' + message, 'warning', 8000);
    }
    retryAudioUploadBtn.style.display = '';
    retryRow.classList.remove('hidden');
    updateTranscribeButton();
    updateSummaryActionsState();
};

// === Audio Health ===
const audioHealthEl = document.getElementById('audioHealth');
const healthMic = document.getElementById('healthMic');
const healthSys = document.getElementById('healthSys');
const healthClipping = document.getElementById('healthClipping');
const healthSilence = document.getElementById('healthSilence');
const healthSilenceText = document.getElementById('healthSilenceText');

window.updateAudioHealth = function(h) {
    // Mic status
    const micDot = healthMic.querySelector('.health-dot');
    if (h.mic_alive) {
        micDot.className = 'health-dot green';
        healthMic.lastChild.textContent = ' Mic active';
    } else {
        micDot.className = 'health-dot red';
        healthMic.lastChild.textContent = ' Mic dead — check connection';
    }
    // Sys status
    const sysDot = healthSys.querySelector('.health-dot');
    if (h.sys_alive) {
        sysDot.className = 'health-dot green';
        healthSys.lastChild.textContent = ' System active';
    } else {
        sysDot.className = 'health-dot yellow';
        healthSys.lastChild.textContent = ' No system audio';
    }
    // Clipping
    if (h.mic_clipping || h.sys_clipping) {
        healthClipping.classList.remove('hidden');
    } else {
        healthClipping.classList.add('hidden');
    }
    // Speech/silence
    if (!h.speech_detected && h.silence_duration > 300) {
        const mins = Math.floor(h.silence_duration / 60);
        healthSilenceText.textContent = 'No speech for ' + mins + ' min';
        healthSilence.classList.remove('hidden');
    } else {
        healthSilence.classList.add('hidden');
    }
};

window.onHealthWarning = function(message) {
    showToast(message, 'warning', 8000);
};

// === Level Monitoring ===
async function startMonitoring() {
    if (isRecording) return;
    const micIdx = micSelect.value !== '' ? parseInt(micSelect.value) : null;
    const loopIdx = loopbackSelect.value !== '' ? parseInt(loopbackSelect.value) : null;
    if (micIdx !== null || loopIdx !== null) {
        await window.pywebview.api.start_monitor(micIdx, loopIdx);
    }
}

micSelect.addEventListener('change', () => {
    micSelect.classList.remove('device-warning-red', 'device-warning-yellow');
    isRecording ? switchSources() : startMonitoring();
});
loopbackSelect.addEventListener('change', () => {
    loopbackSelect.classList.remove('device-warning-red', 'device-warning-yellow');
    isRecording ? switchSources() : startMonitoring();
});

async function switchSources() {
    const micIdx = micSelect.value !== '' ? parseInt(micSelect.value) : null;
    const loopIdx = loopbackSelect.value !== '' ? parseInt(loopbackSelect.value) : null;

    if (micIdx === null && loopIdx === null) {
        showToast('At least one audio source must be selected.', 'warning');
        return;
    }

    try {
        const result = await window.pywebview.api.switch_sources(micIdx, loopIdx);
        if (!result.success) {
            console.error('Failed to switch sources:', result.error);
            setStatus('recording', 'Recording (switch failed)');
            setTimeout(() => { if (isRecording) setStatus('recording', 'Recording'); }, 3000);
        }
    } catch (err) {
        console.error('Failed to switch sources:', err);
    }
}

// === Refresh Devices Button ===
refreshDevicesBtn.addEventListener('click', async () => {
    if (isRecording || transcriptionInProgress) return;
    refreshDevicesBtn.disabled = true;
    refreshDevicesBtn.classList.add('spinning');
    try {
        await autoDetectDevicesOnce({ refreshDevices: true });
    } catch (err) {
        console.error('Failed to refresh devices:', err);
    } finally {
        refreshDevicesBtn.disabled = false;
        refreshDevicesBtn.classList.remove('spinning');
    }
});

// === Step 1: Fibery Meeting Selection ===

// Panel URL change callback (called from Python via SourceChanged)
let _pendingDisambiguationRevalidate = false;
window.onPanelUrlChanged = function(url) {
    panelCurrentUrl = url;
    updateSelectButtonState();
    if (_pendingDisambiguationRevalidate) {
        _pendingDisambiguationRevalidate = false;
        selectMeetingFromPanel();
    }
};

function updateCreateMeetingButtons() {
    const hasName = createMeetingName.value.trim().length > 0;
    document.querySelectorAll('.create-meeting-btn').forEach((button) => {
        if (button.dataset.type === 'interview') {
            button.disabled = false;
        } else {
            button.disabled = !hasName;
        }
    });
}

function extractFiberyEntityCandidateUrl(url) {
    if (!url) return '';
    try {
        const parsed = new URL(url);
        const pathSegments = parsed.pathname.split('/').filter(Boolean);
        if (pathSegments.length >= 3 && /-\d+$/.test(pathSegments[pathSegments.length - 1])) {
            return parsed.href;
        }

        const fragment = parsed.hash.replace(/^#/, '').split('/').filter(Boolean);
        if (fragment.length >= 3 && /-\d+$/.test(fragment[fragment.length - 1])) {
            return `${parsed.origin}/${fragment.join('/')}`;
        }
    } catch {
        return '';
    }
    return '';
}

function looksLikeFiberyEntity(url) {
    return Boolean(extractFiberyEntityCandidateUrl(url));
}

function hasLocalTranscript() {
    return Boolean(window.transcriptManager?.hasContent && window.transcriptManager.hasContent());
}

function hasLinkedTranscript() {
    return Boolean(linkedTranscriptText && linkedTranscriptText.trim().length > 0);
}

function hasEffectiveTranscript() {
    return hasLocalTranscript() || hasLinkedTranscript();
}

function getEffectiveTranscriptText() {
    const localFormatted = window.transcriptManager?.getFormattedText
        ? window.transcriptManager.getFormattedText()
        : '';
    if (localFormatted && localFormatted.trim()) {
        return localFormatted;
    }

    const localText = window.transcriptManager?.getFullText
        ? window.transcriptManager.getFullText()
        : '';
    if (localText && localText.trim()) {
        return localText;
    }

    return hasLinkedTranscript() ? linkedTranscriptText : '';
}

function updateSummaryActionsState(scrollIntoView = false) {
    const hasTranscript = hasEffectiveTranscript();
    const summaryBlockedByCoreTranscription = transcriptionInProgress && !hasCompletedTranscription;
    const shouldShowActions =
        !isRecording &&
        !summaryBlockedByCoreTranscription &&
        (fiberyValidated || hasTranscript);

    sendActions.classList.toggle('visible', shouldShowActions);

    applySummarizeButtonState(hasTranscript);
    copyTranscriptBtn.disabled = !hasTranscript;
    copySummaryBtn.disabled = !generatedSummary;

    if (shouldShowActions && scrollIntoView) {
        sendActions.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

function applySummarizeButtonState(hasTranscript = hasEffectiveTranscript()) {
    summarizeBtn.classList.toggle('processing', summarizeInProgress);

    if (summarizeInProgress) {
        summarizeBtn.disabled = true;
        summarizeBtn.textContent = formatSummarizeLabel();
        return;
    }

    summarizeBtn.disabled = !hasTranscript;
    summarizeBtn.textContent = summarizeRetryPending
        ? 'Retry Summary'
        : (hasCompletedSummary ? 'Resummarize' : 'Summarize');
}

function formatSummarizeLabel() {
    if (!summarizeStartedAt) return 'Summarizing...';

    const elapsedSeconds = Math.max(0, Math.floor((Date.now() - summarizeStartedAt) / 1000));
    if (elapsedSeconds < 10) return 'Summarizing...';

    const mins = Math.floor(elapsedSeconds / 60);
    const secs = elapsedSeconds % 60;
    if (mins > 0) {
        return `Summarizing (${mins}m ${secs}s)...`;
    }
    return `Summarizing (${secs}s)...`;
}

function startSummarizeProgressTimer() {
    stopSummarizeProgressTimer();
    summarizeStartedAt = Date.now();
    summarizeProgressTimer = setInterval(() => {
        applySummarizeButtonState();
    }, 1000);
}

function stopSummarizeProgressTimer() {
    summarizeStartedAt = 0;
    if (summarizeProgressTimer) {
        clearInterval(summarizeProgressTimer);
        summarizeProgressTimer = null;
    }
}

function applyLinkedEntity(result, entityUrl) {
    fiberyValidated = true;
    currentFiberyUrl = entityUrl || result.url || panelCurrentUrl || '';
    currentEntityUrl = result.url || entityUrl || panelCurrentUrl || '';
    currentEntityDb = result.database || '';
    linkedTranscriptText = result.transcript_text || '';
    fiberyDisambiguation.classList.add('hidden');
    fiberyMissingWarning.classList.add('hidden');

    entityName.textContent = result.entity_name || '';
    entityDb.textContent = result.database || '';
    entityLink.href = currentEntityUrl || '#';
    entityLink.title = 'Open in Fibery';
    fiberyEntityInfo.classList.remove('hidden');

    fiberySelectRow.classList.add('hidden');
    fiberySelectHint.classList.add('hidden');
    createMeetingDividerRow.classList.add('hidden');
    createMeetingFields.classList.add('hidden');
    setFiberyValidateStatus('', '');
    updateAudioStorageState();
    sendPanelCollapsible.classList.remove('collapsed');
    updateSummaryActionsState();

    if (hasPreparedAudio()) {
        audioStorageCollapsible.classList.remove('collapsed');
    }

    if (result.pending_summary && sendActions.classList.contains('visible')) {
        setFiberyStatus('Sending summary to Fibery...', '');
    }
}

function updateSelectButtonState() {
    if (fiberyValidated) return; // Already selected, button hidden
    const isEntity = looksLikeFiberyEntity(panelCurrentUrl);
    selectMeetingBtn.disabled = !isEntity;
    if (isEntity) {
        fiberySelectHint.classList.add('hidden');
        fiberySelectRow.classList.remove('hidden');
    } else {
        fiberySelectHint.classList.remove('hidden');
        fiberySelectRow.classList.add('hidden');
    }
}

selectMeetingBtn.addEventListener('click', selectMeetingFromPanel);

async function selectMeetingFromPanel() {
    selectMeetingBtn.disabled = true;
    selectMeetingBtn.textContent = 'Checking...';
    setFiberyValidateStatus('', '');

    try {
        const result = await window.pywebview.api.select_meeting_from_panel();
        if (result.success) {
            applyLinkedEntity(result, extractFiberyEntityCandidateUrl(panelCurrentUrl));

            // Check recording lock
            if (result.recording_lock && result.recording_lock.locked) {
                const proceed = confirm(
                    result.recording_lock.locked_by + ' is already recording this meeting.\n\nDo you want to continue recording?'
                );
                if (proceed) {
                    await callApi('acquire_recording_lock');
                } else {
                    await callApi('deselect_meeting');
                    resetFiberyValidation();
                    showToast('Meeting deselected — another user is recording.', 'info');
                    return;
                }
            }

            await autoDetectDevicesOnce();

        } else if (result.needs_disambiguation) {
            fiberyDisambiguation.classList.remove('hidden');
            disambigOptions.innerHTML = '';
            result.candidates.forEach(candidate => {
                const btn = document.createElement('button');
                btn.className = 'disambig-option';
                btn.innerHTML = `<span class="entity-name">${candidate.entity_name}</span><span class="entity-db">${candidate.database}</span>`;
                btn.addEventListener('click', async () => {
                    fiberyDisambiguation.classList.add('hidden');
                    await callApi('navigate_entity_panel', candidate.url);
                    _pendingDisambiguationRevalidate = true;
                    // Safety timeout: clear flag after 3s if panel never fires
                    setTimeout(() => { _pendingDisambiguationRevalidate = false; }, 3000);
                });
                disambigOptions.appendChild(btn);
            });
        } else {
            setFiberyValidateStatus('Not a valid meeting: ' + (result.error || 'Unknown'), 'error');
        }
    } catch (err) {
        setFiberyValidateStatus('Error: ' + err, 'error');
    } finally {
        selectMeetingBtn.textContent = 'Select current meeting \u2192';
        updateSelectButtonState();
    }
}

// === Create Meeting ===
createMeetingName.addEventListener('input', updateCreateMeetingButtons);

document.querySelectorAll('.create-meeting-btn').forEach(btn => {
    btn.addEventListener('click', () => createMeeting(btn.dataset.type));
});
updateCreateMeetingButtons();

async function createMeeting(meetingType) {
    // Disable all create buttons while working
    const buttons = document.querySelectorAll('.create-meeting-btn');
    buttons.forEach(b => { b.disabled = true; });

    try {
        setFiberyValidateStatus('Creating meeting...', '');
        const meetingName = createMeetingName.value.trim();
        const createName = meetingType === 'interview' && !meetingName ? '-' : meetingName;
        const result = await window.pywebview.api.create_fibery_meeting(meetingType, createName);
        if (result.success) {
            applyLinkedEntity(result, result.url || '');

            // Navigate panel to the new entity
            if (currentEntityUrl) {
                await callApi('navigate_entity_panel', currentEntityUrl);
            }

            // Check recording lock if entity created while recording
            if (result.recording_lock && result.recording_lock.locked) {
                const proceed = confirm(
                    result.recording_lock.locked_by + ' is already recording this meeting.\n\nDo you want to continue recording?'
                );
                if (proceed) {
                    await callApi('acquire_recording_lock');
                } else {
                    await callApi('deselect_meeting');
                    resetFiberyValidation();
                    showToast('Meeting deselected — another user is recording.', 'info');
                    return;
                }
            }
            if (result.warning) {
                showToast(result.warning, 'warning', 8000);
            }

            await autoDetectDevicesOnce();
        } else {
            setFiberyValidateStatus('Error: ' + result.error, 'error');
        }
    } catch (err) {
        setFiberyValidateStatus('Error: ' + err, 'error');
    } finally {
        updateCreateMeetingButtons();
    }
}

function resetFiberyValidation() {
    fiberyValidated = false;
    currentFiberyUrl = '';
    currentEntityUrl = '';
    currentEntityDb = '';
    linkedTranscriptText = '';
    fiberyEntityInfo.classList.add('hidden');
    fiberyDisambiguation.classList.add('hidden');
    fiberySelectRow.classList.remove('hidden');
    fiberySelectHint.classList.remove('hidden');
    createMeetingDividerRow.classList.remove('hidden');
    createMeetingFields.classList.remove('hidden');
    createMeetingName.value = '';
    updateCreateMeetingButtons();
    entityLink.href = '#';
    setFiberyValidateStatus('', '');
    updateSelectButtonState();
    updateAudioStorageState();
    if (!hasLocalTranscript() && !hasPreparedAudio() && !isRecording && !transcriptionInProgress) {
        sendPanelCollapsible.classList.add('collapsed');
    }
    updateSummaryActionsState();
}

changeLinkBtn.addEventListener('click', async () => {
    // Block meeting changes during processing (allowed during recording)
    if (transcriptionInProgress) {
        showToast('Cannot change meeting while processing.', 'warning');
        return;
    }
    await window.pywebview.api.deselect_meeting();
    resetFiberyValidation();
    // Re-collapse toggles when meeting deselected
    if (!hasPreparedAudio()) {
        audioStorageCollapsible.classList.add('collapsed');
    }
    if (!hasLocalTranscript() && !hasPreparedAudio()) {
        sendPanelCollapsible.classList.add('collapsed');
    }
    // Show warning if deselected during recording
    if (isRecording) {
        fiberyMissingWarning.classList.remove('hidden');
    }
});

function setFiberyValidateStatus(text, type) {
    fiberyValidateStatus.textContent = text;
    fiberyValidateStatus.className = 'fibery-status ' + type;
}

// === New Meeting / Reset Session ===
newMeetingBtn.addEventListener('click', async () => {
    if (isRecording || transcriptionInProgress) {
        if (!confirm('Processing is still running. Discarding will lose your transcript. Continue?')) {
            return;
        }
    }
    if (isRecording) await stopRecording();
    resetSession();
});

async function resetSession() {
    // Full reset: clear Python session data (transcript, summary, state)
    await window.pywebview.api.reset_session();
    resetFiberyValidation();

    // Clear transcript DOM so stale data cannot leak into the next session
    window.transcriptManager.clear();
    linkedTranscriptText = '';

    // Reset audio storage to settings default
    const defaultStorage = window._defaultAudioStorage || 'local';
    const storageRadio = document.querySelector(`input[name="audioStorage"][value="${defaultStorage}"]`);
    if (storageRadio) storageRadio.checked = true;
    audioStorageCollapsible.classList.add('collapsed');
    // Reset transcript mode to append
    const appendRadio = document.getElementById('modeAppend');
    if (appendRadio) appendRadio.checked = true;
    syncTranscriptModeInputs('append');
    syncRecordingModeInputs('mic_only');
    const summaryAppendRadio = document.getElementById('summaryModeAppend');
    if (summaryAppendRadio) summaryAppendRadio.checked = true;
    syncSummaryLanguageInputs('en');

    // Reset recording meta and button
    recordingMetaCollapsible.classList.add('collapsed');
    audioHealthEl.classList.add('hidden');
    setAudioSourceToolsHidden(false);
    setStatus('', '');
    recordTimer.textContent = '00:00:00';
    timerAccumulatedMs = 0;

    // Hide new meeting link
    newMeetingBtn.classList.add('hidden');

    // Show upload section, collapse Step 3
    uploadCollapsible.classList.remove('collapsed');
    transcribePanelCollapsible.classList.add('collapsed');
    sendPanelCollapsible.classList.add('collapsed');

    // Clear text fields
    additionalPrompt.value = '';
    createMeetingName.value = '';
    updateCreateMeetingButtons();

    // Reset upload state
    clearPreparedAudioUi();
    clearUploadBtn.disabled = false;
    clearUploadBtn.classList.add('hidden');

    // Reset summary and retry state
    clearSummaryForRetranscribe();
    sendActions.classList.remove('visible');
    copyTranscriptBtn.disabled = true;
    applySummarizeButtonState(false);

    // Clear warnings
    fiberyMissingWarning.classList.add('hidden');
}

// === Recording Controls ===
let _recordActionPending = false;
recordBtn.addEventListener('click', async () => {
    // Debounce: prevent rapid double-click from starting then immediately stopping
    if (_recordActionPending) return;
    // Ignore clicks when button is in processing/completed state
    if (transcriptionInProgress || recordBtn.classList.contains('completed')) return;
    _recordActionPending = true;
    try {
        if (!isRecording) {
            await startRecording();
        } else {
            await stopRecording();
        }
    } finally {
        _recordActionPending = false;
    }
});

// --- Decision Popup Button Handlers ---

document.getElementById('decisionContinueBtn').addEventListener('click', () => {
    document.getElementById('silenceOverlay').classList.remove('open');
    window.pywebview.api.decision_continue_recording();
});

document.getElementById('decisionEndNowBtn').addEventListener('click', () => {
    document.getElementById('silenceOverlay').classList.remove('open');
    window.pywebview.api.decision_end_now();
});

document.getElementById('decisionEndAtBtn').addEventListener('click', () => {
    // If dropdown is visible, use its selected value; otherwise use first (only) checkpoint
    const select = document.getElementById('checkpointSelect');
    const index = select.style.display !== 'none' ? parseInt(select.value) : 0;
    document.getElementById('silenceOverlay').classList.remove('open');
    window.pywebview.api.decision_end_at_checkpoint(index);
});


// --- Transcript Mode Toggle ---
document.querySelectorAll('input[name="transcriptMode"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        applyTranscriptMode(e.target.value);
    });
});

// --- Recording Mode Toggle ---
document.querySelectorAll('input[name="recordingMode"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        applyRecordingMode(e.target.value);
    });
});

// --- Summary Mode Toggle ---
document.querySelectorAll('input[name="summaryMode"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        window.pywebview.api.set_summary_mode(e.target.value);
    });
});

// --- Summary Language Toggle ---
document.querySelectorAll('input[name="summaryLanguage"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        applySummaryLanguage(e.target.value);
    });
});

async function startRecording() {
    const micIdx = micSelect.value !== '' ? parseInt(micSelect.value) : null;
    const loopIdx = loopbackSelect.value !== '' ? parseInt(loopbackSelect.value) : null;

    if (micIdx === null && loopIdx === null) {
        showToast('Please select at least one audio source.', 'warning');
        return;
    }

    // Update UI immediately so the button feels responsive
    isRecording = true;
    setStatus('recording', 'Recording');
    audioHealthEl.classList.remove('hidden');

    // Show recording meta (timer + badge), hide upload section
    recordingMetaCollapsible.classList.remove('collapsed');
    uploadCollapsible.classList.add('collapsed');

    // Hide downstream sections until the file is prepared
    sendPanelCollapsible.classList.remove('collapsed');
    transcribePanelCollapsible.classList.add('collapsed');
    audioStorageCollapsible.classList.add('collapsed');

    timerAccumulatedMs = 0;
    startTimer();

    // Warn if no meeting selected
    if (!fiberyValidated) {
        fiberyMissingWarning.classList.remove('hidden');
    }

    // Check recording lock if Fibery entity is linked
    if (fiberyValidated) {
        try {
            const lockResult = await window.pywebview.api.check_recording_lock();
            if (lockResult.locked) {
                const proceed = confirm(
                    lockResult.locked_by + ' is already recording this meeting.\n\nRecording anyway will duplicate API costs and may overwrite their transcript.\n\nDo you want to record anyway?'
                );
                if (!proceed) {
                    isRecording = false;
                    setStatus('', '');
                    stopTimer();
                    recordingMetaCollapsible.classList.add('collapsed');
                    uploadCollapsible.classList.remove('collapsed');
                    audioStorageCollapsible.classList.add('collapsed');
                    return;
                }
            }
            // Fail closed: abort recording if lock cannot be acquired
            await callApi('acquire_recording_lock');
        } catch (err) {
            isRecording = false;
            setStatus('', '');
            stopTimer();
            recordingMetaCollapsible.classList.add('collapsed');
            uploadCollapsible.classList.remove('collapsed');
            audioStorageCollapsible.classList.add('collapsed');
            sendPanelCollapsible.classList.add('collapsed');
            showToast('Could not acquire recording lock: ' + err, 'error');
            return;
        }
    }

    try {
        await callApi('start_recording', micIdx, loopIdx);
    } catch (err) {
        // Revert UI on failure
        isRecording = false;
        setStatus('', '');
        stopTimer();
        audioHealthEl.classList.add('hidden');
        console.error('Failed to start recording:', err);
        showToast('Failed to start recording: ' + err, 'error');
        fiberyMissingWarning.classList.add('hidden');
        // Revert progressive disclosure
        recordingMetaCollapsible.classList.add('collapsed');
        uploadCollapsible.classList.remove('collapsed');
        sendPanelCollapsible.classList.add('collapsed');
        audioStorageCollapsible.classList.add('collapsed');
        // Release lock if we acquired one
        try { await callApi('release_recording_lock'); } catch (_) {}
    }
}

async function stopRecording() {
    freezeTimer();
    try {
        const result = await callApi('stop_recording');
        // Only transition UI on success
        isRecording = false;
        audioHealthEl.classList.add('hidden');
        if (result.prepared_audio && result.prepared_audio.file_path) {
            clearSummaryForRetranscribe();
            applyPreparedAudio(result.prepared_audio);
        } else {
            setStatus('', '');
            setAudioSourceToolsHidden(false);
            uploadCollapsible.classList.remove('collapsed');
            startMonitoring();
        }
    } catch (err) {
        startTimer();
        // Stop failed — backend is STILL RECORDING. Keep UI in recording state.
        console.error('Failed to stop recording:', err);
        showToast('Failed to stop recording: ' + err, 'error');
        // Keep isRecording=true and timer running — backend is still recording
    }
}

// === Timer ===
function getCurrentTimerMs() {
    if (startTime === null) {
        return timerAccumulatedMs;
    }
    return timerAccumulatedMs + Math.max(0, Date.now() - startTime);
}

function freezeTimer() {
    timerAccumulatedMs = getCurrentTimerMs();
    recordTimer.textContent = formatTime(timerAccumulatedMs);
    stopTimer();
}

function startTimer() {
    stopTimer();
    startTime = Date.now();
    recordTimer.textContent = formatTime(timerAccumulatedMs);
    timerInterval = setInterval(() => {
        recordTimer.textContent = formatTime(getCurrentTimerMs());
    }, 1000);
}

function stopTimer() {
    if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
    }
    startTime = null;
}

function formatTime(ms) {
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s % 60).padStart(2,'0')}`;
}

// === Status (merged into record button) ===
function setStatus(state, text) {
    // Remove all state classes
    recordBtn.classList.remove('recording', 'processing', 'completed');
    if (state) {
        recordBtn.classList.add(state);
    }
    recordBtn.classList.toggle('hidden', state === 'completed');
    // Update button text for non-recording states
    if (state === 'processing') {
        newMeetingBtn.classList.add('hidden');
        recordBtnText.textContent = text || 'Processing...';
    } else if (state === 'completed') {
        recordBtnText.textContent = 'Start Recording';
        // Show new meeting link in header
        newMeetingBtn.classList.remove('hidden');
    } else if (state === 'recording') {
        newMeetingBtn.classList.add('hidden');
        recordBtnText.textContent = 'Stop Recording';
    } else {
        // idle / reset
        newMeetingBtn.classList.add('hidden');
        recordBtnText.textContent = 'Start Recording';
    }
    updateSummaryActionsState();
}

window.onAudioPrepared = function(info) {
    isRecording = false;
    audioHealthEl.classList.add('hidden');
    stopTimer();
    if (info && info.file_path) {
        clearSummaryForRetranscribe();
        applyPreparedAudio(info);
        showToast('Audio is ready to transcribe.', 'info', 4000);
    } else {
        setStatus('', '');
        setAudioSourceToolsHidden(false);
        uploadCollapsible.classList.remove('collapsed');
        startMonitoring();
    }
};

// === Called from Python with progress updates during batch processing ===
window.onProcessingProgress = function(message) {
    transcriptionInProgress = true;
    updateTranscribeButton(message);
};

// === Called from Python when processing completes ===
window.onProcessingComplete = function() {
    transcriptionInProgress = false;
    hasCompletedTranscription = true;
    setStatus('completed');
    updateTranscribeButton();
    showSendActions();
    newMeetingBtn.classList.remove('hidden');

    // Warn if transcript is empty (e.g. very short recording with no speech).
    // Check both cleaned text and raw DOM elements — cleaned text can be empty
    // due to Gemini cleanup even when utterances exist.
    if (window.transcriptManager.hasContent && !window.transcriptManager.hasContent()) {
        showToast('No speech detected in the recording.', 'warning', 8000);
    }

    // Safe to resume level monitoring now that batch processing is done.
    // The background scanner keeps running and will resume its idle checks.
    startMonitoring();

    // Reset upload state but keep section hidden until "New meeting" reset
    clearUploadBtn.disabled = false;
    clearUploadBtn.classList.add('hidden');
    // Upload section stays collapsed — revealed on resetSession()
};

window.onError = function(message) {
    transcriptionInProgress = false;
    updateTranscribeButton();
    clearUploadBtn.disabled = false;
    clearUploadBtn.classList.add('hidden');
    if (hasPreparedAudio()) {
        uploadCollapsible.classList.remove('collapsed');
        setSelectedUploadUiVisible(true);
    }
    showToast(message, 'error', 8000);
    newMeetingBtn.classList.remove('hidden');
};

let _lastFailedWavPath = '';
window.onBatchFailed = function(info) {
    transcriptionInProgress = false;
    updateTranscribeButton();
    sendActions.classList.remove('visible');
    uploadCollapsible.classList.remove('collapsed');
    clearUploadBtn.disabled = false;
    clearUploadBtn.classList.add('hidden');
    if (hasPreparedAudio()) {
        setSelectedUploadUiVisible(true);
    }
    _lastFailedWavPath = (info && info.wav_path) || '';
    if (_lastFailedWavPath) {
        showToast('Transcription failed. Your recording was saved — click Retry to try again.', 'info', 10000);
        retryBatchBtn.style.display = '';
    }
    newMeetingBtn.classList.remove('hidden');
    // Resume idle monitoring
    startMonitoring();
};

// === Decision Popup (silence/sleep) ===

function _formatMeetingTime(totalSeconds) {
    const mins = Math.floor(totalSeconds / 60);
    const secs = Math.floor(totalSeconds % 60);
    return String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
}

function _checkpointLabel(cp) {
    const time = _formatMeetingTime(cp.meetingSecs);
    return cp.type === 'sleep'
        ? 'End at ' + time + ' (before sleep)'
        : 'End at ' + time + ' (before silence)';
}

function _renderCheckpointControls(checkpoints) {
    const endAtBtn = document.getElementById('decisionEndAtBtn');
    const select = document.getElementById('checkpointSelect');
    const group = document.getElementById('decisionCheckpointGroup');

    if (checkpoints.length === 0) {
        group.style.display = 'none';
        group.classList.remove('has-dropdown');
    } else if (checkpoints.length === 1) {
        group.style.display = '';
        group.classList.remove('has-dropdown');
        select.style.display = 'none';
        endAtBtn.style.display = '';
        endAtBtn.textContent = _checkpointLabel(checkpoints[0]);
    } else {
        group.style.display = '';
        group.classList.add('has-dropdown');
        select.style.display = '';
        endAtBtn.style.display = '';
        select.innerHTML = '';
        checkpoints.forEach((cp) => {
            const opt = document.createElement('option');
            opt.value = cp.index;
            opt.textContent = _checkpointLabel(cp);
            select.appendChild(opt);
        });
        // Auto-select latest checkpoint
        select.value = checkpoints[checkpoints.length - 1].index;
        endAtBtn.textContent = 'End';
    }
}

window.onShowDecisionPopup = function(data) {
    // data = {checkpoints: [{type, meetingSecs, index}], currentRecordingSecs, sleepMinutes?}
    const overlay = document.getElementById('silenceOverlay');
    overlay.classList.add('open');

    // Set description based on most recent checkpoint type
    const descEl = document.getElementById('decisionDesc');
    const checkpoints = data.checkpoints || [];
    const lastCp = checkpoints.length > 0 ? checkpoints[checkpoints.length - 1] : null;
    if (data.sleepMinutes) {
        descEl.textContent = 'Your computer was asleep for ' + data.sleepMinutes + ' minute' + (data.sleepMinutes !== 1 ? 's' : '') + '.';
    } else if (lastCp && lastCp.type === 'silence') {
        descEl.textContent = 'No audio has been detected for a while.';
    } else {
        descEl.textContent = 'Recording was paused.';
    }

    // Show milestone recording time
    const milestoneTime = lastCp ? lastCp.meetingSecs : 0;
    document.getElementById('decisionTimer').textContent = _formatMeetingTime(milestoneTime);

    // Freeze the main timer at milestone time
    stopTimer();
    timerAccumulatedMs = milestoneTime * 1000;
    recordTimer.textContent = formatTime(milestoneTime * 1000);

    _renderCheckpointControls(checkpoints);
};

window.onDecisionPopupUpdate = function(data) {
    _renderCheckpointControls(data.checkpoints || []);
};

window.onDecisionPopupDismiss = function() {
    document.getElementById('silenceOverlay').classList.remove('open');
};

window.onDecisionTimerResume = function(accumulatedSeconds) {
    timerAccumulatedMs = accumulatedSeconds * 1000;
    startTimer();
};

window.onAutoStopComplete = function() {
    showToast('Audio is ready to transcribe.', 'info', 5000);
};

// === System Sleep / Wake ===

window.onSleepPauseTimer = function(accumulatedSeconds) {
    // Just freeze the timer — no state change, no UI indication
    stopTimer();
    timerAccumulatedMs = accumulatedSeconds * 1000;
};

window.onWakeResumeTimer = function(accumulatedSeconds) {
    timerAccumulatedMs = accumulatedSeconds * 1000;
    startTimer();
};

window.onWakeResumeFailed = function(errorMsg) {
    isRecording = false;
    stopTimer();
    showToast('Could not resume after sleep: ' + errorMsg, 'warning', 10000);
};

// Called when recording ends and transitions to processing (e.g., after sleep timeout/failure)
window.onRecordingEndedForProcessing = function() {
    showToast('Audio is ready to transcribe.', 'info', 5000);
};

window.onSleepDuringProcessing = function() {
    showToast('Processing may have been interrupted by sleep. If stuck, try New Meeting.', 'warning', 10000);
};

window.onCleanupFailed = function() {
    showToast('Transcript improvement unavailable. Showing the raw transcript.', 'info', 6000);
};

// === Device Scan Results (background scanning) ===
// Python only sends scan results for a device type when the selected source is silent.
// Empty array = selected source has audio, clear warning.
window.onDeviceScanResults = function(scanResults) {
    updateDeviceWarning(micSelect, scanResults.microphones);
    updateDeviceWarning(loopbackSelect, scanResults.loopbacks);
};

function updateDeviceWarning(selectEl, scanResults) {
    selectEl.classList.remove('device-warning-red', 'device-warning-yellow');

    // Empty results = no scan was needed (selected device has audio)
    if (!scanResults || scanResults.length === 0) return;

    const selectedIndex = selectEl.value !== '' ? parseInt(selectEl.value) : null;
    if (selectedIndex === null) return;

    // Selected device is silent (Python only scans when silent).
    // Check if any OTHER device has audio.
    const otherActive = scanResults.filter(
        r => r.device_index !== selectedIndex && r.is_active && !r.scan_failed
    );

    if (otherActive.length > 0) {
        // RED: selected device silent, another device has audio
        selectEl.classList.add('device-warning-red');
    }
    // No yellow case needed - Python only scans when selected is silent
}

// === Step 3: Show action buttons after processing ===
function showSendActions() {
    updateSummaryActionsState(true);
}

function getSummaryStyle() {
    const selected = document.querySelector('input[name="summaryStyle"]:checked');
    return selected ? selected.value : 'normal';
}

// === Transcript auto-send callbacks (triggered from Python after step 2) ===
window.onTranscriptSentToFibery = function() {
    setStatus('completed');
    applyTranscriptMode('replace');
    updateTranscribeButton();
    retryTranscriptBtn.style.display = 'none';
};

window.onFiberyAssigneeWarning = function(message) {
    showToast(message || 'Please update Fibery username in Settings.', 'warning', 8000);
};

window.onTranscriptSendError = function(message) {
    const isEntityDeleted = message && (message.includes('not found') || message.includes('Not found'));
    if (isEntityDeleted) {
        showToast('Meeting was deleted in Fibery. Select a new meeting and retry.', 'error', 10000);
    } else {
        showToast('Could not send transcript to Fibery: ' + message, 'warning', 8000);
    }
    retryTranscriptBtn.style.display = '';
    retryRow.classList.remove('hidden');
    updateTranscribeButton();
};

// === Summarize (step 3) ===
summarizeBtn.addEventListener('click', async () => {
    if (!hasEffectiveTranscript()) {
        setFiberyStatus('No transcript available yet.', 'error');
        return;
    }

    summarizeInProgress = true;
    summarizeRetryPending = false;
    startSummarizeProgressTimer();
    applySummarizeButtonState(true);
    setFiberyStatus('Summarizing...', '');

    try {
        const customPrompt = additionalPrompt.value.trim();
        const summaryStyle = getSummaryStyle();
        // Returns immediately — result arrives via onSummarizeComplete/onSummarizeError
        await window.pywebview.api.generate_summary(
            customPrompt,
            summaryStyle,
            getSelectedSummaryLanguage(),
        );
    } catch (err) {
        summarizeInProgress = false;
        summarizeRetryPending = true;
        stopSummarizeProgressTimer();
        setFiberyStatus('Error: ' + err, 'error');
        updateSummaryActionsState();
    }
});

window.onSummarizeComplete = function(result) {
    summarizeInProgress = false;
    summarizeRetryPending = false;
    stopSummarizeProgressTimer();
    hasCompletedSummary = true;
    generatedSummary = (result && result.summary) ? result.summary : '';
    copySummaryBtn.disabled = !generatedSummary;

    if (result && result.sent_to_fibery) {
        setFiberyStatus('Updated in Fibery', 'success');
    } else if (result && result.fibery_error) {
        setFiberyStatus('Summary ready — Fibery error: ' + result.fibery_error, 'error');
    } else {
        // No link — prompt user to add one
        setFiberyStatus('Fibery link missing', 'error');
    }
    updateSummaryActionsState();
};

window.onSummarizeError = function(message) {
    summarizeInProgress = false;
    summarizeRetryPending = true;
    stopSummarizeProgressTimer();
    setFiberyStatus('Error: ' + message, 'error');
    updateSummaryActionsState();
};

// === Pending summary sent after link was added ===
window.onPendingSummarySent = function() {
    setFiberyStatus('Updated in Fibery', 'success');
};

window.onPendingSummarySendError = function(message) {
    setFiberyStatus('Fibery error: ' + message, 'error');
};

function setFiberyStatus(text, type) {
    summaryStatusBadge.textContent = text || '';
    summaryStatusBadge.className = 'status-badge' + (type ? ' ' + type : '');
    const shouldShowInlineStatus = Boolean(text) && (type === 'error' || type === 'warning');
    summaryStatusRow.classList.toggle('hidden', !shouldShowInlineStatus);
}

// === Retry Handlers ===
retryTranscriptBtn.addEventListener('click', async () => {
    retryTranscriptBtn.disabled = true;
    try {
        await callApi('retry_send_transcript');
        retryTranscriptBtn.style.display = 'none';
    } catch (err) {
        showToast('Retry failed: ' + err, 'error');
    } finally {
        retryTranscriptBtn.disabled = false;
    }
});

retryAudioUploadBtn.addEventListener('click', async () => {
    retryAudioUploadBtn.disabled = true;
    try {
        await callApi('retry_audio_upload');
    } catch (err) {
        showToast('Retry failed: ' + err, 'error');
    } finally {
        retryAudioUploadBtn.disabled = false;
    }
});

retryBatchBtn.addEventListener('click', async () => {
    if (!_lastFailedWavPath) return;
    retryBatchBtn.disabled = true;
    retryBatchBtn.style.display = 'none';
    try {
        transcriptionInProgress = true;
        updateTranscribeButton('Retrying...');
        const result = await callApi(
            'start_transcription',
            false,
            IMPROVE_TRANSCRIPT_WITH_CONTEXT,
            getSelectedTranscriptMode(),
            getSelectedRecordingMode(),
        );
        if (result.effective_recording_mode) {
            syncRecordingModeInputs(result.effective_recording_mode);
        }
        if (result.recording_mode_auto_corrected) {
            const reason = result.recording_mode_reason ? ` ${result.recording_mode_reason}` : '';
            showToast(`Using Mic only for this transcript.${reason}`, 'info', 7000);
        }
        _lastFailedWavPath = '';
    } catch (err) {
        transcriptionInProgress = false;
        updateTranscribeButton();
        showToast('Retry failed: ' + err, 'error');
        retryBatchBtn.disabled = false;
        retryBatchBtn.style.display = '';
    }
});

// === Transcript Actions ===
copyTranscriptBtn.addEventListener('click', () => {
    const text = getEffectiveTranscriptText();
    if (text) {
        navigator.clipboard.writeText(text).then(() => {
            copyTranscriptBtn.textContent = 'Copied!';
            setTimeout(() => { copyTranscriptBtn.textContent = 'Copy Transcript'; }, 2000);
            // Notify Python for close-confirmation logic
            window.pywebview.api.mark_transcript_copied();
        });
    }
});

updateSummaryActionsState();

copySummaryBtn.addEventListener('click', () => {
    if (generatedSummary) {
        navigator.clipboard.writeText(generatedSummary).then(() => {
            copySummaryBtn.textContent = 'Copied!';
            setTimeout(() => { copySummaryBtn.textContent = 'Copy Summary'; }, 2000);
        });
    }
});

// === Update Available Banner ===
window.onUpdateAvailable = function(info) {
    // info: {version, url, notes}
    // Don't show if already dismissed this session
    if (window._updateDismissed) return;

    const existing = document.getElementById('updateBanner');
    if (existing) return;

    const banner = document.createElement('div');
    banner.id = 'updateBanner';
    banner.className = 'update-banner';
    banner.innerHTML = `
        <span>Version ${info.version} is available!</span>
        ${info.url ? `<a href="#" class="update-link" id="updateDownloadLink">Download</a>` : ''}
        <button class="update-dismiss" title="Dismiss">&times;</button>
    `;
    document.body.prepend(banner);

    if (info.url) {
        document.getElementById('updateDownloadLink').addEventListener('click', (e) => {
            e.preventDefault();
            window.pywebview.api.open_url(info.url);
        });
    }
    banner.querySelector('.update-dismiss').addEventListener('click', () => {
        banner.remove();
        window._updateDismissed = true;
    });
};
