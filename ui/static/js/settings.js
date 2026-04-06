/**
 * Settings panel management.
 */
class SettingsManager {
    constructor() {
        this.overlay = document.getElementById('settingsOverlay');
        this.openBtn = document.getElementById('settingsBtn');
        this.closeBtn = document.getElementById('closeSettingsBtn');
        this.saveBtn = document.getElementById('saveSettingsBtn');
        this.saveRecordingsCheckbox = document.getElementById('saveRecordings');
        this.recordingsDirRow = document.getElementById('recordingsDirRow');
        this.recordingsDirInput = document.getElementById('recordingsDir');
        this.browseBtn = document.getElementById('browseRecordingsBtn');
        this.postProcessingCheckbox = document.getElementById('postProcessingEnabled');
        this.postProcessingOptions = document.getElementById('postProcessingOptions');
        this.postProcessingInputs = this.postProcessingOptions
            ? Array.from(this.postProcessingOptions.querySelectorAll('input[type="checkbox"]'))
            : [];

        this.openBtn.addEventListener('click', () => this.open());
        this.closeBtn.addEventListener('click', () => this.close());
        this.saveBtn.addEventListener('click', () => this.save());

        // Close on overlay background click
        this.overlay.addEventListener('click', (e) => {
            if (e.target === this.overlay) this.close();
        });

        // Theme live preview
        document.getElementById('themeSelect').addEventListener('change', (e) => {
            document.body.setAttribute('data-theme', e.target.value);
            // Refresh cached gradient colors for audio visualizers
            if (window.micViz) window.micViz.refreshColors();
            if (window.sysViz) window.sysViz.refreshColors();
        });


        this.postProcessingCheckbox.addEventListener('change', () => {
            this._togglePostProcessingControls();
        });

        // Advanced settings toggle
        const advToggle = document.getElementById('advancedSettingsToggle');
        const advContent = document.getElementById('advancedSettingsContent');
        if (advToggle && advContent) {
            advToggle.addEventListener('click', () => {
                const expanded = advToggle.getAttribute('aria-expanded') === 'true';
                advToggle.setAttribute('aria-expanded', String(!expanded));
                advContent.classList.toggle('collapsed', expanded);
            });
        }

        // Browse folder button
        this.browseBtn.addEventListener('click', () => this.browseFolder());

        // Clear API key links
        this._pendingClears = new Set();
        document.querySelectorAll('.clear-key-link').forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                const keyName = link.dataset.key;
                this._pendingClears.add(keyName);
                // Find the sibling input and mark it
                const input = link.parentElement.querySelector('input');
                if (input) {
                    input.value = '';
                    input.placeholder = 'Will be cleared on save';
                }
            });
        });
    }

    _toggleRecordingsDirRow() {
        this.recordingsDirRow.classList.remove('hidden');
    }

    _togglePostProcessingControls() {
        const enabled = this.postProcessingCheckbox.checked;
        this.postProcessingInputs.forEach((input) => {
            input.disabled = !enabled;
        });
        if (this.postProcessingOptions) {
            this.postProcessingOptions.classList.toggle('is-disabled', !enabled);
        }
    }

    open() {
        this.overlay.classList.add('open');
        this.loadCurrentSettings();
    }

    close() {
        this.overlay.classList.remove('open');
        // Discard unsaved "Clear" actions so they don't leak into a later save
        this._pendingClears.clear();
    }

    async loadCurrentSettings() {
        // Reset stale clears from a previous open that was cancelled
        this._pendingClears.clear();
        try {
            const settings = await window.pywebview.api.get_settings();
            document.getElementById('autoStart').checked = settings.auto_start_on_boot || false;
            document.getElementById('minimizeToTray').checked = settings.minimize_to_tray_on_close || false;
            this.saveRecordingsCheckbox.checked = settings.save_recordings !== false;
            document.getElementById('themeSelect').value = settings.theme || 'dark';
            document.body.setAttribute('data-theme', settings.theme || 'dark');
            document.getElementById('displayName').value = settings.display_name || '';
            this.recordingsDirInput.value = settings.recordings_dir || '';
            this.recordingsDirInput.placeholder = settings.default_recordings_dir || 'Default location';
            this._toggleRecordingsDirRow();
            document.getElementById('defaultAudioStorage').value = settings.audio_storage || 'local';
            document.getElementById('defaultPanelPage').value = settings.default_panel_page || '';
            document.getElementById('noiseSuppression').checked = settings.noise_suppression !== false;
            document.getElementById('agcEnabled').checked = settings.agc !== false;
            document.getElementById('audioTranscriptCleanupEnabled').checked = settings.audio_transcript_cleanup_enabled === true;
            this.postProcessingCheckbox.checked = settings.post_processing === true;
            document.getElementById('echoCancellationEnabled').checked = settings.echo_cancellation === true;
            document.getElementById('postNoiseSuppression').checked = settings.post_noise_suppression === true;
            document.getElementById('postAgcEnabled').checked = settings.post_agc === true;
            document.getElementById('postNormalizeEnabled').checked = settings.post_normalize === true;
            this._togglePostProcessingControls();

            // Gemini model settings
            document.getElementById('settingsGeminiModel').value = settings.gemini_model || '';
            document.getElementById('settingsGeminiFallback').value = settings.gemini_model_fallback || '';
            document.getElementById('settingsGeminiCleanup').value = settings.gemini_model_cleanup || '';
            // Company context
            document.getElementById('settingsCompanyContext').value = settings.company_context || settings.default_company_context || '';

            // Show API key status (filled = configured, empty placeholder = not set)
            const keysStatus = await window.pywebview.api.get_api_keys_status();
            document.getElementById('settingsAssemblyAI').placeholder = keysStatus.assemblyai ? 'Configured' : 'Not set';
            document.getElementById('settingsGemini').placeholder = keysStatus.gemini ? 'Configured' : 'Not set';
            document.getElementById('settingsFibery').placeholder = keysStatus.fibery ? 'Configured' : 'Not set';
            // Clear values — only show placeholder status
            document.getElementById('settingsAssemblyAI').value = '';
            document.getElementById('settingsGemini').value = '';
            document.getElementById('settingsFibery').value = '';
        } catch (err) {
            console.error('Failed to load settings:', err);
        }
    }

    async browseFolder() {
        try {
            const result = await window.pywebview.api.browse_folder();
            if (result.success) {
                this.recordingsDirInput.value = result.path;
            }
        } catch (err) {
            console.error('Failed to browse folder:', err);
        }
    }

    async save() {
        const geminiModel = document.getElementById('settingsGeminiModel').value.trim();
        const geminiFallback = document.getElementById('settingsGeminiFallback').value.trim();
        const geminiCleanup = document.getElementById('settingsGeminiCleanup').value.trim();
        const settings = {
            auto_start_on_boot: document.getElementById('autoStart').checked,
            minimize_to_tray_on_close: document.getElementById('minimizeToTray').checked,
            save_recordings: this.saveRecordingsCheckbox.checked,
            recordings_dir: this.recordingsDirInput.value,
            theme: document.getElementById('themeSelect').value,
            display_name: document.getElementById('displayName').value.trim(),
            gemini_model: geminiModel,
            gemini_model_fallback: geminiFallback,
            gemini_model_cleanup: geminiCleanup,
            company_context: document.getElementById('settingsCompanyContext').value,
            audio_storage: document.getElementById('defaultAudioStorage').value,
            default_panel_page: document.getElementById('defaultPanelPage').value.trim(),
            noise_suppression: document.getElementById('noiseSuppression').checked,
            agc: document.getElementById('agcEnabled').checked,
            audio_transcript_cleanup_enabled: document.getElementById('audioTranscriptCleanupEnabled').checked,
            post_processing: this.postProcessingCheckbox.checked,
            echo_cancellation: document.getElementById('echoCancellationEnabled').checked,
            post_noise_suppression: document.getElementById('postNoiseSuppression').checked,
            post_agc: document.getElementById('postAgcEnabled').checked,
            post_normalize: document.getElementById('postNormalizeEnabled').checked,
        };

        try {
            const saveResult = await window.pywebview.api.save_settings(settings);
            if (saveResult && saveResult.warning) {
                showToast(saveResult.warning, 'warning', 8000);
            }

            // Update the cached default audio storage
            window._defaultAudioStorage = settings.audio_storage || 'local';

            // Sync the Step 2 audio storage radio with the saved default
            const storageRadio = document.querySelector(`input[name="audioStorage"][value="${settings.audio_storage}"]`);
            if (storageRadio && !storageRadio.disabled) storageRadio.checked = true;

            // Save API keys if any were entered (non-empty fields)
            const keys = {};
            const aai = document.getElementById('settingsAssemblyAI').value.trim();
            const gem = document.getElementById('settingsGemini').value.trim();
            const fib = document.getElementById('settingsFibery').value.trim();
            if (aai) keys.assemblyai_api_key = aai;
            if (gem) keys.gemini_api_key = gem;
            if (fib) keys.fibery_api_token = fib;
            // Include __CLEAR__ sentinel for keys marked for deletion
            for (const keyName of this._pendingClears) {
                if (!keys[keyName]) keys[keyName] = '__CLEAR__';
            }
            this._pendingClears.clear();
            if (Object.keys(keys).length > 0) {
                const result = await window.pywebview.api.save_api_keys(keys);
                if (result.warning) {
                    showToast(result.warning, 'warning', 8000);
                }
            }

            this.close();
        } catch (err) {
            console.error('Failed to save settings:', err);
            showToast('Failed to save settings: ' + err, 'error');
        }
    }
}

window.settingsManager = new SettingsManager();
