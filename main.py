"""Fibery Transcript - Entry point.

Lightweight cross-platform audio transcription tool.
Captures mic + speaker audio, transcribes with speaker diarization,
and integrates with Fibery.io for meeting summarization.
"""

import logging
import sys
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.fiberytranscript.app")

from app import FiberyTranscriptApp
from config.settings import Settings
from ui.api_bridge import ApiBridge
from ui.window import create_window, start_webview
from utils.logging_config import setup_logging
from utils.platform_utils import get_data_dir


def main():
    # Initialize
    data_dir = get_data_dir()
    setup_logging(data_dir)

    logger = logging.getLogger(__name__)
    logger.info("Fibery Transcript starting...")
    logger.info("Data directory: %s", data_dir)

    # Load settings
    settings_path = data_dir / "settings.json"
    settings = Settings.load(settings_path)

    # Create app
    app = FiberyTranscriptApp(settings, data_dir)

    # Create API bridge
    bridge = ApiBridge(app)

    # Create window
    window = create_window(bridge, confirm_close=False)
    app.window = window

    # Create entity side panel (attached to main window)
    from ui.entity_panel import EntityPanel
    app.entity_panel = EntityPanel(window)

    # Start power monitoring for sleep/wake detection
    from utils.power_monitor import create_power_monitor
    power_monitor = create_power_monitor(
        on_sleep=app.on_system_sleep,
        on_wake=app.on_system_wake,
    )
    if power_monitor:
        power_monitor.start()
        app._power_monitor = power_monitor

    def _on_closing():
        """Conditionally show quit confirmation, or minimize to tray."""
        # Minimize to tray instead of closing, unless shutting down
        if app.settings.minimize_to_tray_on_close and not app._is_shutting_down:
            window.hide()
            return False  # Prevent window destruction

        if app.needs_close_confirmation:
            # Enable the native confirm dialog; pywebview checks this
            # right after the closing event, so toggling it here works.
            # Do NOT begin_shutdown yet — user may cancel the dialog.
            window.confirm_close = True
            return  # begin_shutdown will run in _on_closed
        window.confirm_close = False
        app.begin_shutdown()

    def _on_closed():
        """Ensure cleanup runs after the window is actually closed."""
        app.begin_shutdown()

    window.events.closing += _on_closing
    window.events.closed += _on_closed

    # Set up system tray (optional, in background)
    _setup_tray(app, window)

    # Start webview event loop (blocks until window closes)
    logger.info("Starting UI...")
    debug = "--debug" in sys.argv
    start_webview(debug=debug)

    # Ensure cleanup ran (begin_shutdown is also called by the closing event,
    # but call again in case the window was destroyed without firing it).
    app.begin_shutdown()
    app.stop_background_scanning()

    logger.info("FiberyTranscript exiting.")

    # Force-exit to avoid pythonnet finalizer crash on Windows.
    # All cleanup has already run above; the .NET runtime's finalizer thread
    # can race against Python interpreter teardown, causing a harmless but
    # noisy NullReferenceException.  os._exit() skips atexit handlers and
    # finalizers, preventing the crash.
    if sys.platform == "win32":
        import os
        os._exit(0)


def _setup_tray(app, window):
    """Set up system tray icon."""
    try:
        from ui.tray import SystemTray

        def on_show():
            window.show()
            window.restore()

        def on_quit():
            # Clean up
            app.begin_shutdown()
            app.stop_background_scanning()
            if app.audio_capture.is_capturing():
                app.audio_capture.stop_capture()
            window.destroy()

        def on_toggle_recording():
            if app.state == FiberyTranscriptApp.STATE_RECORDING:
                app.stop_recording()
            else:
                # Can't start from tray without device selection
                window.show()
                window.restore()

        tray = SystemTray(on_show, on_quit, on_toggle_recording)
        tray.create()
    except Exception as e:
        logging.getLogger(__name__).warning("System tray not available: %s", e)


if __name__ == "__main__":
    main()
