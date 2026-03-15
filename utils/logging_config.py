"""Logging configuration."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.constants import APP_NAME


def setup_logging(data_dir: Path | None = None, level: int = logging.INFO) -> None:
    """Configure logging to console and optionally to a rotating file."""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Rotating file handler (5 MB max, 3 backups)
    if data_dir:
        log_file = data_dir / f"{APP_NAME.lower()}.log"
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
