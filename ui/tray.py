"""System tray icon management using pystray."""

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class SystemTray:
    """System tray icon with context menu."""

    def __init__(
        self,
        on_show: Callable,
        on_quit: Callable,
        on_toggle_recording: Callable,
    ):
        self._on_show = on_show
        self._on_quit = on_quit
        self._on_toggle_recording = on_toggle_recording
        self._icon = None
        self._is_recording = False

    def create(self) -> None:
        """Create and start the system tray icon in a background thread."""
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = self._load_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("Show", self._on_show, default=True),
                pystray.MenuItem(
                    lambda item: "Stop Recording" if self._is_recording else "Start Recording",
                    self._on_toggle_recording,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._on_quit),
            )
            self._icon = pystray.Icon("FiberyTranscript", image, "FiberyTranscript", menu)
            threading.Thread(target=self._icon.run, daemon=True).start()
            logger.info("System tray icon created")
        except Exception as e:
            logger.warning("Failed to create system tray icon: %s", e)

    def _load_icon_image(self):
        """Load the app icon from file, falling back to generated icon."""
        try:
            from PIL import Image
            from utils.platform_utils import get_resource_path
            icon_path = get_resource_path("ui/static/icon.png")
            return Image.open(icon_path).resize((64, 64))
        except Exception:
            return self._create_icon_image(recording=False)

    def set_recording(self, is_recording: bool) -> None:
        """Update tray icon to reflect recording state."""
        self._is_recording = is_recording
        if self._icon:
            self._icon.icon = self._create_icon_image(recording=is_recording)

    def stop(self) -> None:
        """Stop and remove the tray icon."""
        if self._icon:
            self._icon.stop()

    def _create_icon_image(self, recording: bool = False):
        """Generate a simple icon image programmatically."""
        from PIL import Image, ImageDraw

        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Background circle
        bg_color = (239, 83, 80, 255) if recording else (79, 195, 247, 255)
        draw.ellipse([4, 4, size - 4, size - 4], fill=bg_color)

        # Inner icon: mic shape (simplified)
        center = size // 2
        draw.rounded_rectangle(
            [center - 6, 14, center + 6, 36],
            radius=6,
            fill=(255, 255, 255, 255),
        )
        draw.arc([center - 10, 28, center + 10, 48], 0, 180, fill=(255, 255, 255, 255), width=2)
        draw.line([center, 48, center, 54], fill=(255, 255, 255, 255), width=2)

        return img
