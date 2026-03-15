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
let fiberyValidated = false;      // true once the link has been validated
let currentFiberyUrl = '';        // the validated URL
let generatedSummary = '';        // cached summary text from last successful summarize
let silenceCountdownInterval = null;
let silenceCountdownRemaining = 0;
let selectedUploadPath = null;    // path to browsed audio file
let currentEntityDb = '';         // entity database name (for Files support check)

// --- DOM elements ---
const recordBtn = document.getElementById('recordBtn');
const recordBtnText = document.getElementById('recordBtnText');
const recordTimer = document.getElementById('recordTimer');
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
const sendPanelCollapsible = document.getElementById('sendPanelCollapsible');
const newMeetingBtn = document.getElementById('newMeetingBtn');
const continueRecordingBanner = document.getElementById('continueRecordingBanner');
const continueRecordingBtn = document.getElementById('continueRecordingBtn');

// Open entity link in external browser (pywebview doesn't support target=_blank)
document.getElementById('entityLink').addEventListener('click', (e) => {
    e.preventDefault();
    if (currentEntityUrl) {
        window.pywebview.api.open_url(currentEntityUrl);
    }
});

// Step 2 – Upload controls
const browseAudioBtn = document.getElementById('browseAudioBtn');
const uploadFileInfoEl = document.getElementById('uploadFileInfo');
const uploadFileName = document.getElementById('uploadFileName');
const uploadFileMeta = document.getElementById('uploadFileMeta');
const clearUploadBtn = document.getElementById('clearUploadBtn');
const transcribeBtn = document.getElementById('transcribeBtn');
const uploadDivider = document.getElementById('uploadDivider');
const uploadControls = document.getElementById('uploadControls');
const audioStorageHint = document.getElementById('audioStorageHint');

// Step 3 – AI summary
const additionalPrompt = document.getElementById('additionalPrompt');
const sendActions = document.getElementById('sendActions');
const summarizeBtn = document.getElementById('summarizeBtn');
const summaryStatusBadge = document.getElementById('summaryStatusBadge');
const copyTranscriptBtn = document.getElementById('copyTranscriptBtn');
const copySummaryBtn = document.getElementById('copySummaryBtn');

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
    await autoSelectDevices();
    startMonitoring();
    window.pywebview.api.start_background_scanning();

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

async function loadDevices() {
    try {
        const devices = await window.pywebview.api.get_audio_devices();

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
    } catch (err) {
        console.error('Failed to load devices:', err);
    }
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
    } else if (currentEntityDb === 'Market Interview') {
        fiberyRadio.disabled = true;
        audioStorageHint.textContent = 'Not available for interviews';
        if (fiberyRadio.checked) localRadio.checked = true;
    } else {
        fiberyRadio.disabled = false;
        audioStorageHint.textContent = '';
    }
}

document.querySelectorAll('input[name="audioStorage"]').forEach(radio => {
    radio.addEventListener('change', (e) => {
        window.pywebview.api.save_settings({ audio_storage: e.target.value });
    });
});

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

        selectedUploadPath = filePath;

        // Show Step 3 (AI Summary) when file is selected
        sendPanelCollapsible.classList.remove('collapsed');

        // Show file info
        const fileName = filePath.replace(/\\/g, '/').split('/').pop();
        uploadFileName.textContent = fileName;
        uploadFileMeta.textContent = formatAudioFileInfo(validation);
        uploadFileInfoEl.classList.remove('hidden');
        transcribeBtn.classList.remove('hidden');
        browseAudioBtn.classList.add('hidden');
    } catch (err) {
        showToast('Error: ' + err, 'error');
    }
});

clearUploadBtn.addEventListener('click', () => {
    selectedUploadPath = null;
    uploadFileInfoEl.classList.add('hidden');
    transcribeBtn.classList.add('hidden');
    browseAudioBtn.classList.remove('hidden');
});

transcribeBtn.addEventListener('click', async () => {
    if (!selectedUploadPath) return;

    transcribeBtn.disabled = true;
    transcribeBtn.textContent = 'Starting...';
    setStatus('processing', 'Processing');

    // Warn if no meeting selected
    if (!fiberyValidated) {
        fiberyMissingWarning.classList.remove('hidden');
    }

    try {
        const result = await window.pywebview.api.upload_and_transcribe(selectedUploadPath);
        if (!result.success) {
            showToast('Failed: ' + result.error, 'error');
            setStatus('', 'Error');
            transcribeBtn.disabled = false;
            transcribeBtn.textContent = 'Transcribe';
            return;
        }
        // Hide upload section during processing
        uploadCollapsible.classList.add('collapsed');
    } catch (err) {
        showToast('Error: ' + err, 'error');
        setStatus('', 'Error');
        transcribeBtn.disabled = false;
        transcribeBtn.textContent = 'Transcribe';
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
    showToast('Audio recording uploaded to Fibery', 'success');
};

window.onAudioUploadError = function(message) {
    showToast('Audio upload to Fibery failed: ' + message, 'warning', 8000);
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
    if (isRecording) return;
    refreshDevicesBtn.disabled = true;
    refreshDevicesBtn.classList.add('spinning');
    const currentMic = micSelect.value;
    const currentLoop = loopbackSelect.value;

    // Re-initialize audio backends to detect newly connected devices
    try {
        const devices = await window.pywebview.api.refresh_audio_devices();
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
    } catch (err) {
        console.error('Failed to refresh devices:', err);
        await loadDevices();
    }

    if (micSelect.querySelector(`option[value="${currentMic}"]`)) micSelect.value = currentMic;
    if (loopbackSelect.querySelector(`option[value="${currentLoop}"]`)) loopbackSelect.value = currentLoop;
    refreshDevicesBtn.disabled = false;
    refreshDevicesBtn.classList.remove('spinning');
    startMonitoring();
});

// === Step 1: Fibery Meeting Selection ===

// Panel URL change callback (called from Python via SourceChanged)
window.onPanelUrlChanged = function(url) {
    panelCurrentUrl = url;
    updateSelectButtonState();
};

function looksLikeFiberyEntity(url) {
    if (!url) return false;
    try {
        const u = new URL(url);
        if (!u.hostname.endsWith('fibery.io')) return false;
        const segments = u.pathname.split('/').filter(Boolean);
        // Need at least 2 segments: Space/entity-slug-NNN
        if (segments.length < 2) return false;
        // Last segment should end with -<digits> (Fibery entity URL pattern)
        return /-\d+$/.test(segments[segments.length - 1]);
    } catch {
        return false;
    }
}

function updateSelectButtonState() {
    if (fiberyValidated) return; // Already selected, button hidden
    const isEntity = looksLikeFiberyEntity(panelCurrentUrl);
    selectMeetingBtn.disabled = !isEntity;
    if (isEntity) {
        fiberySelectHint.classList.add('hidden');
    } else {
        fiberySelectHint.classList.remove('hidden');
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
            fiberyValidated = true;
            currentFiberyUrl = panelCurrentUrl;
            currentEntityUrl = panelCurrentUrl;
            currentEntityDb = result.database || '';
            fiberyDisambiguation.classList.add('hidden');
            fiberyMissingWarning.classList.add('hidden');

            entityName.textContent = result.entity_name;
            entityDb.textContent = result.database;
            entityLink.href = currentEntityUrl;
            entityLink.title = 'Open in Fibery';
            fiberyEntityInfo.classList.remove('hidden');

            fiberySelectRow.classList.add('hidden');
            fiberySelectHint.classList.add('hidden');
            createMeetingDividerRow.classList.add('hidden');
            createMeetingFields.classList.add('hidden');
            setFiberyValidateStatus('', '');
            updateAudioStorageState();

            // Show audio storage if recording is active
            if (isRecording) {
                audioStorageCollapsible.classList.remove('collapsed');
            }

            // Check recording lock
            if (result.recording_lock && result.recording_lock.locked) {
                const proceed = confirm(
                    result.recording_lock.locked_by + ' is already recording this meeting.\n\nDo you want to continue recording?'
                );
                if (proceed) window.pywebview.api.acquire_recording_lock();
            }

            if (result.pending_summary && sendActions.classList.contains('visible')) {
                setFiberyStatus('Sending summary to Fibery...', '');
            }
        } else if (result.needs_disambiguation) {
            fiberyDisambiguation.classList.remove('hidden');
            disambigOptions.innerHTML = '';
            result.candidates.forEach(candidate => {
                const btn = document.createElement('button');
                btn.className = 'disambig-option';
                btn.innerHTML = `<span class="entity-name">${candidate.entity_name}</span><span class="entity-db">${candidate.database}</span>`;
                btn.addEventListener('click', async () => {
                    fiberyDisambiguation.classList.add('hidden');
                    await window.pywebview.api.navigate_entity_panel(candidate.url);
                    setTimeout(() => selectMeetingFromPanel(), 500);
                });
                disambigOptions.appendChild(btn);
            });
        } else {
            setFiberyValidateStatus('Not a valid meeting: ' + (result.error || 'Unknown'), 'error');
        }
    } catch (err) {
        setFiberyValidateStatus('Error: ' + err, 'error');
    } finally {
        selectMeetingBtn.textContent = 'Select meeting';
        updateSelectButtonState();
    }
}

// === Create Meeting ===
createMeetingName.addEventListener('input', () => {
    const hasName = createMeetingName.value.trim().length > 0;
    document.querySelectorAll('.create-meeting-btn').forEach(b => { b.disabled = !hasName; });
});

document.querySelectorAll('.create-meeting-btn').forEach(btn => {
    btn.addEventListener('click', () => createMeeting(btn.dataset.type));
});

async function createMeeting(meetingType) {
    // Disable all create buttons while working
    const buttons = document.querySelectorAll('.create-meeting-btn');
    buttons.forEach(b => { b.disabled = true; });
    setFiberyValidateStatus('Creating meeting...', '');

    try {
        const result = await window.pywebview.api.create_fibery_meeting(meetingType, createMeetingName.value.trim());
        if (result.success) {
            fiberyValidated = true;
            currentFiberyUrl = result.url || '';
            currentEntityUrl = result.url || '';
            currentEntityDb = result.database || '';
            fiberyDisambiguation.classList.add('hidden');
            fiberyMissingWarning.classList.add('hidden');

            entityName.textContent = result.entity_name;
            entityDb.textContent = result.database;
            if (currentEntityUrl) {
                entityLink.href = currentEntityUrl;
                entityLink.title = 'Open in Fibery';
            }
            fiberyEntityInfo.classList.remove('hidden');
            createMeetingDividerRow.classList.add('hidden');
            createMeetingFields.classList.add('hidden');
            setFiberyValidateStatus('', '');

            // Navigate panel to the new entity
            if (currentEntityUrl) {
                window.pywebview.api.navigate_entity_panel(currentEntityUrl);
            }
            fiberySelectRow.classList.add('hidden');
            fiberySelectHint.classList.add('hidden');
            updateAudioStorageState();

            // Show audio storage if recording is active
            if (isRecording) {
                audioStorageCollapsible.classList.remove('collapsed');
            }

            // Check recording lock if entity created while recording
            if (result.recording_lock && result.recording_lock.locked) {
                const proceed = confirm(
                    result.recording_lock.locked_by + ' is already recording this meeting.\n\nDo you want to continue recording?'
                );
                if (proceed) {
                    window.pywebview.api.acquire_recording_lock();
                }
            }
        } else {
            setFiberyValidateStatus('Error: ' + result.error, 'error');
        }
    } catch (err) {
        setFiberyValidateStatus('Error: ' + err, 'error');
    } finally {
        buttons.forEach(b => { b.disabled = !createMeetingName.value.trim(); });
    }
}

function resetFiberyValidation() {
    fiberyValidated = false;
    currentFiberyUrl = '';
    currentEntityUrl = '';
    currentEntityDb = '';
    fiberyEntityInfo.classList.add('hidden');
    fiberyDisambiguation.classList.add('hidden');
    fiberySelectRow.classList.remove('hidden');
    fiberySelectHint.classList.remove('hidden');
    createMeetingDividerRow.classList.remove('hidden');
    createMeetingFields.classList.remove('hidden');
    createMeetingName.value = '';
    document.querySelectorAll('.create-meeting-btn').forEach(b => { b.disabled = true; });
    entityLink.href = '#';
    setFiberyValidateStatus('', '');
    updateSelectButtonState();
    updateAudioStorageState();
}

changeLinkBtn.addEventListener('click', async () => {
    await window.pywebview.api.deselect_meeting();
    fiberyEntityInfo.classList.add('hidden');
    fiberySelectRow.classList.remove('hidden');
    fiberySelectHint.classList.remove('hidden');
    createMeetingDividerRow.classList.remove('hidden');
    createMeetingFields.classList.remove('hidden');
    createMeetingName.value = '';
    document.querySelectorAll('.create-meeting-btn').forEach(b => { b.disabled = true; });
    fiberyValidated = false;
    currentFiberyUrl = '';
    currentEntityUrl = '';
    currentEntityDb = '';
    entityLink.href = '#';
    setFiberyValidateStatus('', '');
    updateSelectButtonState();
    updateAudioStorageState();
    // Re-collapse audio storage when meeting deselected
    audioStorageCollapsible.classList.add('collapsed');
});

function setFiberyValidateStatus(text, type) {
    fiberyValidateStatus.textContent = text;
    fiberyValidateStatus.className = 'fibery-status ' + type;
}

// === New Meeting / Reset Session ===
newMeetingBtn.addEventListener('click', async () => {
    if (isRecording) {
        await stopRecording();
    }
    resetSession();
});

// === Continue Recording after sleep ===
continueRecordingBtn.addEventListener('click', async () => {
    const micIdx = micSelect.value !== '' ? parseInt(micSelect.value) : null;
    const loopIdx = loopbackSelect.value !== '' ? parseInt(loopbackSelect.value) : null;

    if (micIdx === null && loopIdx === null) {
        showToast('Please select at least one audio source.', 'warning');
        return;
    }

    const result = await window.pywebview.api.continue_recording(micIdx, loopIdx);
    if (result.success) {
        continueRecordingBanner.classList.add('hidden');
        isRecording = true;
        setStatus('recording', 'Recording');
        recordingMetaCollapsible.classList.remove('collapsed');
        startTimer();
        newMeetingBtn.classList.add('hidden');
        showToast('Recording resumed. Transcripts will be merged.', 'info', 5000);
    } else {
        showToast('Failed to continue recording: ' + (result.error || 'Unknown error'), 'error');
    }
});

async function resetSession() {
    // Full reset: clear Python session data (transcript, summary, state)
    await window.pywebview.api.reset_session();
    resetFiberyValidation();

    // Reset audio storage to settings default
    const defaultStorage = window._defaultAudioStorage || 'local';
    const storageRadio = document.querySelector(`input[name="audioStorage"][value="${defaultStorage}"]`);
    if (storageRadio) storageRadio.checked = true;
    audioStorageCollapsible.classList.add('collapsed');

    // Reset recording meta and button
    recordingMetaCollapsible.classList.add('collapsed');
    setStatus('', '');
    recordTimer.textContent = '00:00:00';

    // Hide new meeting link
    newMeetingBtn.classList.add('hidden');

    // Show upload section, collapse Step 3
    uploadCollapsible.classList.remove('collapsed');
    sendPanelCollapsible.classList.add('collapsed');

    // Clear text fields
    additionalPrompt.value = '';
    createMeetingName.value = '';

    // Reset upload state
    selectedUploadPath = null;
    uploadFileInfoEl.classList.add('hidden');
    transcribeBtn.classList.add('hidden');
    transcribeBtn.disabled = false;
    transcribeBtn.textContent = 'Transcribe';
    browseAudioBtn.classList.remove('hidden');

    // Reset summary state
    generatedSummary = '';
    sendActions.classList.remove('visible');
    summaryStatusBadge.textContent = '';
    copySummaryBtn.disabled = true;

    // Clear warnings
    fiberyMissingWarning.classList.add('hidden');

    // Hide continue recording banner
    continueRecordingBanner.classList.add('hidden');
}

// === Recording Controls ===
let _recordActionPending = false;
recordBtn.addEventListener('click', async () => {
    // Debounce: prevent rapid double-click from starting then immediately stopping
    if (_recordActionPending) return;
    // Ignore clicks when button is in processing/completed state
    if (recordBtn.classList.contains('processing') || recordBtn.classList.contains('completed')) return;
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

document.getElementById('silenceDismissBtn').addEventListener('click', () => {
    if (silenceCountdownInterval) {
        clearInterval(silenceCountdownInterval);
        silenceCountdownInterval = null;
    }
    document.getElementById('silenceOverlay').classList.remove('open');
    window.pywebview.api.dismiss_silence_countdown();
});

document.getElementById('silenceStopNowBtn').addEventListener('click', () => {
    if (silenceCountdownInterval) {
        clearInterval(silenceCountdownInterval);
        silenceCountdownInterval = null;
    }
    document.getElementById('silenceOverlay').classList.remove('open');
    stopRecording();
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

    // Show recording meta (timer + badge), hide upload section
    recordingMetaCollapsible.classList.remove('collapsed');
    uploadCollapsible.classList.add('collapsed');

    // Show Step 3 (AI Summary)
    sendPanelCollapsible.classList.remove('collapsed');

    // Show audio storage if meeting is linked
    if (fiberyValidated) {
        audioStorageCollapsible.classList.remove('collapsed');
    }

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
            await window.pywebview.api.acquire_recording_lock();
        } catch (err) {
            console.warn('Recording lock check failed, proceeding anyway:', err);
        }
    }

    try {
        await callApi('stop_background_scanning');
        await callApi('start_recording', micIdx, loopIdx);
    } catch (err) {
        // Revert UI on failure
        isRecording = false;
        setStatus('', '');
        stopTimer();
        console.error('Failed to start recording:', err);
        showToast('Failed to start recording: ' + err, 'error');
        fiberyMissingWarning.classList.add('hidden');
        // Revert progressive disclosure
        recordingMetaCollapsible.classList.add('collapsed');
        uploadCollapsible.classList.remove('collapsed');
        audioStorageCollapsible.classList.add('collapsed');
        // Release lock if we acquired one
        try { await callApi('release_recording_lock'); } catch (_) {}
    }
}

async function stopRecording() {
    try {
        await callApi('stop_recording');
        // Only transition UI on success
        isRecording = false;
        stopTimer();
        setStatus('processing', 'Processing...');
    } catch (err) {
        // Stop failed — backend is STILL RECORDING. Keep UI in recording state.
        console.error('Failed to stop recording:', err);
        showToast('Failed to stop recording: ' + err, 'error');
        // Keep isRecording=true and timer running — backend is still recording
    }
}

// === Timer ===
function startTimer() {
    startTime = Date.now();
    timerInterval = setInterval(() => {
        recordTimer.textContent = formatTime(Date.now() - startTime);
    }, 1000);
}

function stopTimer() {
    if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
    }
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
    // Update button text for non-recording states
    if (state === 'processing') {
        recordBtnText.textContent = text || 'Processing...';
    } else if (state === 'completed') {
        recordBtnText.textContent = text || 'Completed';
        // Show new meeting link in header
        newMeetingBtn.classList.remove('hidden');
    } else if (state === 'recording') {
        recordBtnText.textContent = 'Stop Recording';
    } else {
        // idle / reset
        recordBtnText.textContent = 'Start Recording';
    }
}

// === Called from Python with progress updates during batch processing ===
window.onProcessingProgress = function(message) {
    recordBtnText.textContent = message;
};

// === Called from Python when processing completes ===
window.onProcessingComplete = function() {
    setStatus('completed', 'Done');
    showSendActions();
    newMeetingBtn.classList.remove('hidden');

    // Enable continue recording button if banner is visible (sleep interruption)
    if (!continueRecordingBanner.classList.contains('hidden')) {
        continueRecordingBtn.disabled = false;
        continueRecordingBtn.textContent = 'Continue Recording';
    }

    // Warn if transcript is empty (e.g. very short recording with no speech)
    if (window.transcriptManager.getFullText().length === 0) {
        showToast('No speech detected in the recording.', 'warning', 8000);
    }

    // Safe to resume level monitoring now that batch processing is done
    startMonitoring();
    window.pywebview.api.start_background_scanning();

    // Reset upload state but keep section hidden until "New meeting" reset
    selectedUploadPath = null;
    uploadFileInfoEl.classList.add('hidden');
    transcribeBtn.classList.add('hidden');
    transcribeBtn.disabled = false;
    transcribeBtn.textContent = 'Transcribe';
    browseAudioBtn.classList.remove('hidden');
    // Upload section stays collapsed — revealed on resetSession()
};

window.onError = function(message) {
    setStatus('', '');
    showToast(message, 'error', 8000);
    newMeetingBtn.classList.remove('hidden');
};

window.onBatchFailed = function(info) {
    setStatus('', '');
    sendActions.classList.remove('visible');
    uploadCollapsible.classList.remove('collapsed');
    if (info.wav_path) {
        showToast('Your recording was saved. You can retry via Upload.', 'info', 10000);
    }
    newMeetingBtn.classList.remove('hidden');
};

// === Silence Auto-Stop ===

window.onSilenceCountdownStart = function(seconds) {
    silenceCountdownRemaining = seconds;
    const overlay = document.getElementById('silenceOverlay');
    const countdownEl = document.getElementById('silenceCountdown');

    countdownEl.textContent = seconds;
    overlay.classList.add('open');

    silenceCountdownInterval = setInterval(() => {
        silenceCountdownRemaining--;
        countdownEl.textContent = silenceCountdownRemaining;

        if (silenceCountdownRemaining <= 0) {
            clearInterval(silenceCountdownInterval);
            silenceCountdownInterval = null;
            overlay.classList.remove('open');
            window.pywebview.api.auto_stop_from_silence();
        }
    }, 1000);
};

window.onSilenceCountdownCancel = function() {
    if (silenceCountdownInterval) {
        clearInterval(silenceCountdownInterval);
        silenceCountdownInterval = null;
    }
    document.getElementById('silenceOverlay').classList.remove('open');
};

window.onAutoStopComplete = function() {
    isRecording = false;
    stopTimer();
    setStatus('processing', 'Processing...');
    showToast('Recording auto-stopped due to silence. Audio file saved.', 'info', 8000);
};

// === System Sleep / Wake ===

window.onSleepStop = function() {
    isRecording = false;
    stopTimer();
    setStatus('processing', 'Processing...');
};

window.onSleepWakeNotification = function() {
    showToast('Recording interrupted by laptop sleep. Audio file saved.', 'warning', 10000);
    // Show continue recording banner (button disabled until processing completes)
    continueRecordingBanner.classList.remove('hidden');
    continueRecordingBtn.disabled = true;
    continueRecordingBtn.textContent = 'Processing...';
};

window.onSleepDuringProcessing = function() {
    showToast('Processing may have been interrupted by sleep. If stuck, try New Meeting.', 'warning', 10000);
};

window.onCleanupFailed = function() {
    showToast('Speaker identification unavailable. Showing raw transcript.', 'info', 6000);
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
    summarizeBtn.disabled = false;
    copySummaryBtn.disabled = true; // enabled once summary is generated
    sendActions.classList.add('visible');
    sendActions.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function getSummaryStyle() {
    const selected = document.querySelector('input[name="summaryStyle"]:checked');
    return selected ? selected.value : 'normal';
}

// === Transcript auto-send callbacks (triggered from Python after step 2) ===
window.onTranscriptSentToFibery = function() {
    setStatus('completed', 'Transcript sent');
};

window.onTranscriptSendError = function(message) {
    showToast('Could not send transcript to Fibery: ' + message, 'warning', 8000);
};

// === Summarize (step 3) ===
summarizeBtn.addEventListener('click', async () => {
    const hasTranscript = window.transcriptManager.getFullText().length > 0;
    if (!hasTranscript) {
        setFiberyStatus('No transcript available yet.', 'error');
        return;
    }

    summarizeBtn.disabled = true;
    setFiberyStatus('Summarizing...', '');

    try {
        const customPrompt = additionalPrompt.value.trim();
        const summaryStyle = getSummaryStyle();
        // Returns immediately — result arrives via onSummarizeComplete/onSummarizeError
        await window.pywebview.api.generate_summary(customPrompt, summaryStyle);
    } catch (err) {
        setFiberyStatus('Error: ' + err, 'error');
        summarizeBtn.disabled = false;
    }
});

window.onSummarizeComplete = function(result) {
    generatedSummary = (result && result.summary) ? result.summary : '';
    copySummaryBtn.disabled = !generatedSummary;
    summarizeBtn.disabled = false;

    if (result && result.sent_to_fibery) {
        setFiberyStatus('Updated in Fibery', 'success');
    } else if (result && result.fibery_error) {
        setFiberyStatus('Summary ready — Fibery error: ' + result.fibery_error, 'error');
    } else {
        // No link — prompt user to add one
        setFiberyStatus('Fibery link missing', 'error');
    }
};

window.onSummarizeError = function(message) {
    setFiberyStatus('Error: ' + message, 'error');
    summarizeBtn.disabled = false;
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
}

// === Transcript Actions ===
copyTranscriptBtn.addEventListener('click', () => {
    const text = window.transcriptManager.getFormattedText() ||
                 window.transcriptManager.getFullText();
    if (text) {
        navigator.clipboard.writeText(text).then(() => {
            copyTranscriptBtn.textContent = 'Copied!';
            setTimeout(() => { copyTranscriptBtn.textContent = 'Copy Transcript'; }, 2000);
        });
    }
});

copySummaryBtn.addEventListener('click', () => {
    if (generatedSummary) {
        navigator.clipboard.writeText(generatedSummary).then(() => {
            copySummaryBtn.textContent = 'Copied!';
            setTimeout(() => { copySummaryBtn.textContent = 'Copy Summary'; }, 2000);
        });
    }
});

// === Device Auto-Refresh ===
setInterval(async () => {
    if (!isRecording && !recordBtn.classList.contains('processing') && !recordBtn.classList.contains('completed')) {
        const currentMic = micSelect.value;
        const currentLoop = loopbackSelect.value;
        await loadDevices();
        if (micSelect.querySelector(`option[value="${currentMic}"]`)) micSelect.value = currentMic;
        if (loopbackSelect.querySelector(`option[value="${currentLoop}"]`)) loopbackSelect.value = currentLoop;
    }
}, 10000);
