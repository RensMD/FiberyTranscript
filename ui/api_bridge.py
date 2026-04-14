"""Python-to-JavaScript API bridge for pywebview.

This class is exposed to the frontend via `window.pywebview.api`.
All public methods are callable from JavaScript.
"""

import logging
import threading
from dataclasses import asdict
from typing import Optional

logger = logging.getLogger(__name__)


class ApiBridge:
    """Exposed to JavaScript via pywebview.api."""

    def __init__(self, app):
        """
        Args:
            app: The FiberyTranscriptApp instance.
        """
        self._app = app

    # --- Audio Devices ---

    def get_audio_devices(self) -> dict:
        """Return available audio devices for mic and loopback."""
        capture = self._app.audio_capture
        mics = capture.list_input_devices()
        loopbacks = capture.list_loopback_devices()
        return {
            "microphones": [d.to_dict() for d in mics],
            "loopbacks": [d.to_dict() for d in loopbacks],
        }

    def refresh_audio_devices(self) -> dict:
        """Re-initialize audio backends and return fresh device list."""
        try:
            capture = self._app.audio_capture
            capture.reinitialize()
            return self.get_audio_devices()
        except Exception as e:
            logger.error("Failed to refresh audio devices: %s", e)
            return {"microphones": [], "loopbacks": [], "error": str(e)}

    # --- Level Monitoring ---

    def start_monitor(self, mic_index: Optional[int] = None, loopback_index: Optional[int] = None) -> dict:
        """Start audio level monitoring (no recording)."""
        try:
            self._app.start_monitor(mic_index, loopback_index)
            return {"success": True}
        except Exception as e:
            logger.error("Failed to start monitor: %s", e)
            return {"success": False, "error": str(e)}

    def stop_monitor(self) -> dict:
        """Stop audio level monitoring."""
        try:
            self._app.stop_monitor()
            return {"success": True}
        except Exception as e:
            logger.error("Failed to stop monitor: %s", e)
            return {"success": False, "error": str(e)}

    # --- Device Scanning ---

    def scan_devices(self) -> dict:
        """Scan all audio devices for activity. Returns scan results."""
        try:
            return self._app.scan_devices()
        except Exception as e:
            logger.error("Device scan failed: %s", e)
            return {"microphones": [], "loopbacks": []}

    def start_background_scanning(self) -> dict:
        """Start periodic background device scanning."""
        try:
            self._app.start_background_scanning()
            return {"success": True}
        except Exception as e:
            logger.error("Failed to start background scanning: %s", e)
            return {"success": False, "error": str(e)}

    def stop_background_scanning(self) -> dict:
        """Stop periodic background device scanning."""
        try:
            self._app.stop_background_scanning()
            return {"success": True}
        except Exception as e:
            logger.error("Failed to stop background scanning: %s", e)
            return {"success": False, "error": str(e)}

    # --- Recording ---

    def start_recording(self, mic_index: Optional[int], loopback_index: Optional[int]) -> dict:
        """Start audio capture and recording."""
        try:
            self._app.start_recording(mic_index, loopback_index)
            return {"success": True}
        except Exception as e:
            logger.error("Failed to start recording: %s", e)
            return {"success": False, "error": str(e)}

    def switch_sources(self, mic_index: Optional[int], loopback_index: Optional[int]) -> dict:
        """Switch audio sources while recording continues."""
        try:
            self._app.switch_sources(mic_index, loopback_index)
            return {"success": True}
        except Exception as e:
            logger.error("Failed to switch sources: %s", e)
            return {"success": False, "error": str(e)}

    def stop_recording(self) -> dict:
        """Stop recording and stage the result for transcription."""
        try:
            info = self._app.stop_recording()
            return {"success": True, "prepared_audio": info or {}}
        except Exception as e:
            logger.error("Failed to stop recording: %s", e)
            return {"success": False, "error": str(e)}

    def decision_continue_recording(self) -> dict:
        """Called by JS when user clicks 'Continue Recording' on decision popup."""
        try:
            self._app.decision_continue_recording()
            return {"success": True}
        except Exception as e:
            logger.error("Decision continue failed: %s", e)
            return {"success": False, "error": str(e)}

    def decision_end_now(self) -> dict:
        """Called by JS when user clicks 'End Meeting Now' on decision popup."""
        try:
            self._app.decision_end_now()
            return {"success": True}
        except Exception as e:
            logger.error("Decision end now failed: %s", e)
            return {"success": False, "error": str(e)}

    def decision_end_at_checkpoint(self, index: int) -> dict:
        """Called by JS when user picks a checkpoint to process up to."""
        try:
            self._app.decision_end_at_checkpoint(int(index))
            return {"success": True}
        except Exception as e:
            logger.error("Decision end at checkpoint failed: %s", e)
            return {"success": False, "error": str(e)}

    def set_transcript_mode(self, mode: str) -> dict:
        """Set transcript mode to 'append' or 'replace' for this meeting."""
        if mode not in ("append", "replace"):
            return {"success": False, "error": "Invalid mode"}
        self._app._transcript_mode = mode
        logger.info("Transcript mode set to: %s", mode)
        return {"success": True}

    def get_transcript_mode(self) -> dict:
        """Get current transcript mode."""
        return {"success": True, "mode": self._app._transcript_mode}

    def set_recording_mode(self, mode: str) -> dict:
        """Set recording mode for the current staged audio."""
        if mode not in ("mic_only", "mic_and_speakers"):
            return {"success": False, "error": "Invalid mode"}
        self._app._recording_mode = mode
        logger.info("Recording mode set to: %s", mode)
        return {"success": True}

    def get_recording_mode(self) -> dict:
        """Get current recording mode."""
        return {"success": True, "mode": self._app._recording_mode}

    def set_summary_mode(self, mode: str) -> dict:
        """Set summary mode to 'append' or 'replace' for this meeting."""
        if mode not in ("append", "replace"):
            return {"success": False, "error": "Invalid mode"}
        self._app._summary_mode = mode
        logger.info("Summary mode set to: %s", mode)
        return {"success": True}

    def get_summary_mode(self) -> dict:
        """Get current summary mode."""
        return {"success": True, "mode": self._app._summary_mode}

    def set_summary_language(self, language: str) -> dict:
        """Set summary output language for this meeting."""
        if language not in ("en", "nl"):
            return {"success": False, "error": "Invalid language"}
        self._app._summary_language = language
        logger.info("Summary language set to: %s", language)
        return {"success": True}

    def get_summary_language(self) -> dict:
        """Get current summary output language."""
        return {"success": True, "language": self._app._summary_language}

    # --- File Upload (Browse & Transcribe) ---

    @staticmethod
    def _get_file_dialog_type(webview_module, kind: str):
        """Return the best available pywebview file-dialog enum value."""
        file_dialog = getattr(webview_module, "FileDialog", None)
        if file_dialog is not None:
            modern_name = "OPEN" if kind == "open" else "FOLDER"
            modern_value = getattr(file_dialog, modern_name, None)
            if modern_value is not None:
                return modern_value

        legacy_name = "OPEN_DIALOG" if kind == "open" else "FOLDER_DIALOG"
        legacy_value = getattr(webview_module, legacy_name, None)
        if legacy_value is None:
            raise RuntimeError(f"pywebview dialog type '{kind}' is unavailable")
        return legacy_value

    def browse_audio_file(self) -> dict:
        """Open a native file picker dialog for audio files."""
        try:
            import webview

            file_types = (
                "Audio files (*.wav;*.mp3;*.ogg;*.flac;*.m4a;*.aac;*.wma;*.webm)",
            )
            result = self._app.window.create_file_dialog(
                self._get_file_dialog_type(webview, "open"),
                file_types=file_types,
            )
            if result and len(result) > 0:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": "No file selected"}
        except Exception as e:
            logger.error("File dialog failed: %s", e)
            return {"success": False, "error": str(e)}

    def validate_audio_file(self, file_path: str) -> dict:
        """Validate an audio file and return info (format, duration, size)."""
        from pathlib import Path

        try:
            info = self._app._validate_audio_file(Path(file_path))
            return {"success": True, **info}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error("Audio validation failed: %s", e)
            return {"success": False, "error": str(e)}

    def prepare_uploaded_audio(self, file_path: str) -> dict:
        """Validate and stage an uploaded audio file."""
        try:
            info = self._app.prepare_uploaded_audio(file_path)
            return {"success": True, "prepared_audio": info}
        except Exception as e:
            logger.error("Upload preparation failed: %s", e)
            return {"success": False, "error": str(e)}

    def start_transcription(
        self,
        remove_echo: bool = False,
        improve_with_context: bool = True,
        transcript_mode: str = "append",
        recording_mode: str = "mic_only",
    ) -> dict:
        """Start transcription for the currently staged audio file."""
        try:
            from app import TranscriptionOptions

            result = self._app.start_transcription(TranscriptionOptions(
                remove_echo=bool(remove_echo),
                improve_with_context=bool(improve_with_context),
                transcript_mode=transcript_mode,
                recording_mode=recording_mode,
            ))
            return result
        except Exception as e:
            logger.error("Transcription start failed: %s", e)
            return {"success": False, "error": str(e)}

    def clear_prepared_audio(self) -> dict:
        """Discard staged audio while keeping the current meeting link."""
        try:
            self._app.clear_prepared_audio()
            return {"success": True}
        except Exception as e:
            logger.error("Failed to clear prepared audio: %s", e)
            return {"success": False, "error": str(e)}

    def upload_and_transcribe(self, file_path: str) -> dict:
        """Compatibility wrapper for legacy callers."""
        try:
            self._app.upload_and_transcribe(file_path)
            return {"success": True}
        except Exception as e:
            logger.error("Upload transcription failed: %s", e)
            return {"success": False, "error": str(e)}

    # --- Settings ---

    def browse_folder(self) -> dict:
        """Open a native folder picker dialog and return the selected path."""
        try:
            import webview
            result = self._app.window.create_file_dialog(
                self._get_file_dialog_type(webview, "folder")
            )
            if result and len(result) > 0:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": "No folder selected"}
        except Exception as e:
            logger.error("Folder dialog failed: %s", e)
            return {"success": False, "error": str(e)}

    def get_settings(self) -> dict:
        """Return current settings as a dictionary."""
        settings = asdict(self._app.settings)
        # Include the resolved default recordings path for display
        if not settings.get("company_context"):
            from config.constants import DEFAULT_COMPANY_CONTEXT
            settings["default_company_context"] = DEFAULT_COMPANY_CONTEXT
        if not settings.get("recordings_dir"):
            settings["default_recordings_dir"] = str(self._app.data_dir / "recordings")
        return settings

    _ALLOWED_SETTINGS = {
        "preferred_mic_device": str,
        "preferred_loopback_device": str,
        "auto_start_on_boot": bool,
        "minimize_to_tray_on_close": bool,
        "theme": str,
        "save_recordings": bool,
        "recordings_dir": str,
        "gemini_model": str,
        "gemini_model_fallback": str,
        "gemini_model_cleanup": str,
        "display_name": str,
        "company_context": str,
        "audio_storage": str,
        "default_panel_page": str,
        "noise_suppression": bool,
        "agc": bool,
        "audio_transcript_cleanup_enabled": bool,
        "post_processing": bool,
        "echo_cancellation": bool,
        "post_noise_suppression": bool,
        "post_agc": bool,
        "post_normalize": bool,
    }

    def save_settings(self, settings_dict: dict) -> dict:
        """Update and persist settings (only whitelisted keys with correct types)."""
        try:
            for key, value in settings_dict.items():
                if key not in self._ALLOWED_SETTINGS:
                    logger.warning("Rejected unknown setting key: %s", key)
                    continue
                expected_type = self._ALLOWED_SETTINGS[key]
                if not isinstance(value, expected_type):
                    logger.warning("Rejected setting %s: expected %s, got %s", key, expected_type.__name__, type(value).__name__)
                    continue
                setattr(self._app.settings, key, value)
            self._app.save_settings()
            # Apply autostart if it was changed
            autostart_warning = None
            if 'auto_start_on_boot' in settings_dict:
                from utils.autostart import set_autostart
                new_autostart = self._app.settings.auto_start_on_boot
                if not set_autostart(new_autostart):
                    action = "enable" if new_autostart else "disable"
                    logger.warning("Failed to %s autostart", action)
                    autostart_warning = f"Settings saved, but failed to {action} start-on-boot. You may need to configure this manually in your OS settings."
            if autostart_warning:
                return {"success": True, "warning": autostart_warning}
            return {"success": True}
        except Exception as e:
            logger.error("Failed to save settings: %s", e)
            return {"success": False, "error": str(e)}

    # --- Recording Lock ---

    def check_recording_lock(self) -> dict:
        """Check if another user is recording this meeting."""
        try:
            return self._app.check_recording_lock()
        except Exception as e:
            logger.error("Recording lock check failed: %s", e)
            return {"locked": False, "error": str(e)}

    def acquire_recording_lock(self) -> dict:
        """Acquire the recording lock (user chose to proceed)."""
        try:
            success = self._app.acquire_recording_lock()
            return {"success": success}
        except Exception as e:
            logger.error("Recording lock acquire failed: %s", e)
            return {"success": False, "error": str(e)}

    def release_recording_lock(self) -> dict:
        """Release the recording lock for the current entity."""
        try:
            self._app.release_recording_lock()
            return {"success": True}
        except Exception as e:
            logger.error("Recording lock release failed: %s", e)
            return {"success": False, "error": str(e)}

    # --- Retry ---

    def retry_send_transcript(self) -> dict:
        """Retry sending transcript to Fibery."""
        try:
            return self._app.retry_send_transcript()
        except Exception as e:
            logger.error("Retry transcript send failed: %s", e)
            return {"success": False, "error": str(e)}

    def retry_audio_upload(self) -> dict:
        """Retry uploading audio to Fibery."""
        try:
            return self._app.retry_audio_upload()
        except Exception as e:
            logger.error("Retry audio upload failed: %s", e)
            return {"success": False, "error": str(e)}

    # --- Fibery ---

    def open_url(self, url: str) -> dict:
        """Open a URL in the default browser."""
        import webbrowser
        try:
            webbrowser.open(url)
            return {'success': True}
        except Exception as e:
            logger.error('Failed to open URL: %s', e)
            return {'success': False, 'error': str(e)}

    def open_entity_panel(self, url: str = "") -> dict:
        """Open the Fibery entity side panel with a URL or the default workspace URL."""
        try:
            if self._app.entity_panel is not None:
                if url and url.strip():
                    self._app.entity_panel.open(url)
                else:
                    self._app.entity_panel.open_default()
                return {'success': True}
            return {'success': False, 'error': 'Entity panel not initialized'}
        except Exception as e:
            logger.error('Failed to open entity panel: %s', e)
            return {'success': False, 'error': str(e)}

    def navigate_entity_panel(self, url: str) -> dict:
        """Navigate the already-open entity panel to a URL."""
        try:
            if self._app.entity_panel is not None:
                self._app.entity_panel.open(url)  # open() handles navigate-if-open
                return {'success': True}
            return {'success': False, 'error': 'Entity panel not initialized'}
        except Exception as e:
            logger.error('Failed to navigate entity panel: %s', e)
            return {'success': False, 'error': str(e)}

    def select_meeting_from_panel(self) -> dict:
        """Get current panel URL and validate it as a Fibery entity."""
        try:
            if self._app.entity_panel is None:
                return {'success': False, 'error': 'Entity panel not initialized'}
            url = self._app.entity_panel.get_current_url()
            if not url:
                return {'success': False, 'error': 'No URL in panel'}
            return self._app.validate_fibery_url(url)
        except Exception as e:
            logger.error('Failed to select meeting from panel: %s', e)
            return {'success': False, 'error': str(e)}

    def deselect_meeting(self) -> dict:
        """Deselect the current meeting (unlink without closing panel)."""
        try:
            self._app.deselect_meeting()
            return {'success': True}
        except Exception as e:
            logger.error('Failed to deselect meeting: %s', e)
            return {'success': False, 'error': str(e)}

    def reset_session(self) -> dict:
        """Full session reset: clear transcript, summary, state. Called by New Meeting."""
        try:
            self._app.reset_session()
            return {'success': True}
        except Exception as e:
            logger.error('Failed to reset session: %s', e)
            return {'success': False, 'error': str(e)}

    def get_session_snapshot(self) -> dict:
        """Return backend session state for frontend reconciliation."""
        try:
            snapshot = self._app.get_session_snapshot()
            return {"success": True, **snapshot}
        except Exception as e:
            logger.error("Failed to get session snapshot: %s", e)
            return {"success": False, "error": str(e)}

    def reset_session_keep_meeting(self) -> dict:
        """Reset workflow outputs while keeping the linked meeting context."""
        try:
            self._app.reset_session_keep_meeting()
            return {"success": True}
        except Exception as e:
            logger.error("Failed to reset session while keeping meeting: %s", e)
            return {"success": False, "error": str(e)}

    def stash_session_undo_snapshot(self, ttl_seconds: int = 15) -> dict:
        """Stash the current workflow state so the next replacement can be undone."""
        try:
            result = self._app.stash_session_undo_snapshot(ttl_seconds)
            return {"success": True, **result}
        except Exception as e:
            logger.error("Failed to stash undo snapshot: %s", e)
            return {"success": False, "error": str(e)}

    def undo_session_replace(self) -> dict:
        """Undo the most recent replacement workflow if still available."""
        try:
            snapshot = self._app.undo_session_replace()
            return {"success": True, "snapshot": snapshot}
        except Exception as e:
            logger.error("Failed to undo session replace: %s", e)
            return {"success": False, "error": str(e)}

    def create_fibery_meeting(self, meeting_type: str, name: str) -> dict:
        """Create a new meeting entity in Fibery."""
        try:
            result = self._app.create_fibery_meeting(meeting_type, name)
            return result
        except Exception as e:
            logger.error('Failed to create Fibery meeting: %s', e)
            return {'success': False, 'error': str(e)}

    def validate_fibery_url(self, fibery_url: str) -> dict:
        """Validate a Fibery URL and return entity name/info."""
        try:
            result = self._app.validate_fibery_url(fibery_url)
            return result
        except Exception as e:
            logger.error("Fibery URL validation failed: %s", e)
            return {"success": False, "error": str(e)}

    def generate_summary(
        self,
        prompt_types=None,
        custom_prompt: str = "",
        summary_style: str = "normal",
        summary_language: str = "en",
    ) -> dict:
        """Generate AI summary from transcript (runs in background). Result via onSummarizeComplete/onSummarizeError."""
        # pywebview may serialize JS arrays as lists; guard against string serialization
        if isinstance(prompt_types, str):
            import json as _json
            try:
                prompt_types = _json.loads(prompt_types)
            except Exception:
                prompt_types = [prompt_types]

        def _background():
            import json
            try:
                result = self._app.generate_summary(
                    prompt_types=prompt_types or ["summarize"],
                    custom_prompt=custom_prompt,
                    summary_style=summary_style,
                    summary_language=summary_language,
                )
                if result.get("success"):
                    self._app._notify_js(
                        f"window.onSummarizeComplete({json.dumps(result)})"
                    )
                else:
                    self._app._notify_js(
                        f"window.onSummarizeError({json.dumps(result.get('error', 'Unknown error'))})"
                    )
            except Exception as e:
                logger.error("Summary generation failed: %s", e)
                self._app._notify_js(
                    f"window.onSummarizeError({json.dumps(str(e))})"
                )

        threading.Thread(target=_background, daemon=True).start()
        return {"success": True, "message": "Processing in background"}

    def summarize_to_fibery(
        self,
        fibery_url: str,
        prompt_types=None,
        custom_prompt: str = "",
        summary_style: str = "normal",
        summary_language: str = "en",
    ) -> dict:
        """Summarize transcript with Gemini and update AI Summary in Fibery (runs in background)."""
        if isinstance(prompt_types, str):
            import json as _json
            try:
                prompt_types = _json.loads(prompt_types)
            except Exception:
                prompt_types = [prompt_types]

        def _background():
            import json
            try:
                result = self._app.send_summary_to_fibery(
                    fibery_url,
                    prompt_types=prompt_types or ["summarize"],
                    custom_prompt=custom_prompt,
                    summary_style=summary_style,
                    summary_language=summary_language,
                )
                if result.get("success"):
                    self._app._notify_js("window.onSummarizeComplete({})")
                else:
                    self._app._notify_js(
                        f"window.onSummarizeError({json.dumps(result.get('error', 'Unknown error'))})"
                    )
            except Exception as e:
                logger.error("Fibery summarize failed: %s", e)
                self._app._notify_js(
                    f"window.onSummarizeError({json.dumps(str(e))})"
                )

        threading.Thread(target=_background, daemon=True).start()
        return {"success": True, "message": "Processing in background"}

    def check_problems_ready(self) -> dict:
        """Re-check Fibery for notes/transcript on the current Market Interview entity."""
        return self._app.check_problems_ready()

    def generate_problems(self) -> dict:
        """Extract problems from the linked Market Interview and create them in Fibery (runs in background).

        Result arrives via window.onProblemsComplete / window.onProblemsError.
        """
        def _background():
            import json
            try:
                result = self._app.generate_problems()
                if result.get("success"):
                    self._app._notify_js(
                        f"window.onProblemsComplete({json.dumps(result)})"
                    )
                else:
                    self._app._notify_js(
                        f"window.onProblemsError({json.dumps(result.get('error', 'Unknown error'))})"
                    )
            except Exception as e:
                logger.error("Problem generation failed: %s", e)
                self._app._notify_js(
                    f"window.onProblemsError({json.dumps(str(e))})"
                )

        threading.Thread(target=_background, daemon=True).start()
        return {"success": True, "message": "Processing in background"}

    # --- API Keys ---

    def get_api_keys_status(self) -> dict:
        """Check if all API keys are configured."""
        from config.keystore import keys_configured, get_all_keys
        keys = get_all_keys()
        return {
            "configured": keys_configured(),
            "assemblyai": bool(keys["assemblyai_api_key"]),
            "gemini": bool(keys["gemini_api_key"]),
            "fibery": bool(keys["fibery_api_token"]),
        }

    def save_api_keys(self, keys: dict) -> dict:
        """Save API keys to the system keyring. Use "__CLEAR__" to delete a key."""
        from config.keystore import save_all_keys, delete_key
        try:
            # Handle __CLEAR__ sentinel for key deletion
            to_delete = [k for k, v in keys.items() if v == "__CLEAR__"]
            for key_name in to_delete:
                delete_key(key_name)
                del keys[key_name]
            success = save_all_keys(keys) if keys else True
            if not success:
                return {"success": True, "warning": "API keys could not be saved to your system keychain. They may not persist after restart."}
            return {"success": True}
        except Exception as e:
            logger.error("Failed to save API keys: %s", e)
            return {"success": False, "error": str(e)}

    def mark_transcript_copied(self) -> dict:
        """Called by JS when the user copies the transcript (for close-confirmation logic)."""
        try:
            if self._app._session:
                self._app._session.results.set_user_has_copied()
            return {"success": True}
        except Exception as e:
            logger.error("mark_transcript_copied failed: %s", e)
            return {"success": False, "error": str(e)}

    # --- State ---

    def get_session_state(self) -> str:
        """Return current session state: idle, recording, processing, completed."""
        return self._app.state
