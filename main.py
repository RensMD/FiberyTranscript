"""Fibery Transcript - Entry point.

Lightweight cross-platform audio transcription tool.
Captures mic + speaker audio, transcribes with speaker diarization,
and integrates with Fibery.io for meeting summarization.
"""

import logging
import sys

if sys.platform == "win32":
    import ctypes

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.fiberytranscript.app")

from app import FiberyTranscriptApp
from config.settings import Settings
from ui.api_bridge import ApiBridge
from ui.window import create_window, start_webview
from utils.logging_config import setup_logging
from utils.platform_utils import get_data_dir
from utils.single_instance import acquire_single_instance_guard


def main():
    # Initialize
    data_dir = get_data_dir()
    setup_logging(data_dir)

    logger = logging.getLogger(__name__)
    logger.info("Fibery Transcript starting...")
    logger.info("Data directory: %s", data_dir)

    instance_guard = acquire_single_instance_guard()
    if instance_guard is None:
        logger.info("Duplicate launch detected; exiting without starting a second instance")
        return

    try:
        # Load settings
        settings_path = data_dir / "settings.json"
        settings = Settings.load(settings_path)

        # Merge installer preferences (written by the Windows installer on install/upgrade)
        if settings.merge_installer_prefs(data_dir):
            logger.info("Merged installer preferences into settings")
            settings.save(settings_path)

        # Create app
        app = FiberyTranscriptApp(settings, data_dir)

        # Create API bridge
        bridge = ApiBridge(app)

        # Create window
        window = create_window(bridge, confirm_close=False)
        app.window = window

        # Create entity side panel (attached to main window)
        from ui.entity_panel import EntityPanel

        app.entity_panel = EntityPanel(window, settings=app.settings)

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
            # Consume the tray-quit flag so it does not persist after a cancelled dialog
            tray_quit = getattr(app, "_tray_quit_requested", False)
            app._tray_quit_requested = False

            # Minimize to tray instead of closing, unless shutting down or tray-quit
            if (
                app.settings.minimize_to_tray_on_close
                and not app._is_shutting_down
                and not tray_quit
            ):
                window.hide()
                return False  # Prevent window destruction

            if app.needs_close_confirmation:
                # Enable the native confirm dialog; pywebview checks this
                # right after the closing event, so toggling it here works.
                # Do not begin shutdown yet - user may cancel the dialog.
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
        # noisy NullReferenceException. os._exit() skips atexit handlers and
        # finalizers, preventing the crash.
        if sys.platform == "win32":
            import os

            instance_guard.release()
            os._exit(0)
    finally:
        instance_guard.release()


def _setup_tray(app, window):
    """Set up system tray icon."""
    try:
        from ui.tray import SystemTray

        def on_show():
            window.show()
            window.restore()

        def on_quit():
            # Set flag so _on_closing() bypasses minimize-to-tray.
            # The flag is consumed (cleared) inside _on_closing() so it
            # does not persist if the user cancels a confirmation dialog.
            app._tray_quit_requested = True

            if app.needs_close_confirmation:
                # Show window so user can see the state before deciding
                window.show()
                window.restore()
            else:
                app.begin_shutdown()

            try:
                window.destroy()
            except Exception:
                pass

        def on_toggle_recording():
            if app.state == FiberyTranscriptApp.STATE_RECORDING:
                app.stop_recording()
            else:
                # Can not start from tray without device selection
                window.show()
                window.restore()

        tray = SystemTray(on_show, on_quit, on_toggle_recording)
        tray.create()
    except Exception as e:
        logging.getLogger(__name__).warning("System tray not available: %s", e)


if __name__ == "__main__":
    main()
