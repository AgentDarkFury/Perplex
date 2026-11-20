"""
Entry point.

    python -m perplex           console mode (logs to the terminal)
    python -m perplex --tray    system tray app (what Perplex.exe runs)
"""

from __future__ import annotations

import sys

from loguru import logger

from .app import Perplex, setup_logging
from .config import load_config
from .paths import config_path, is_frozen, log_path


def console() -> None:
    path = config_path()

    try:
        config = load_config(path)
    except Exception as e:
        logger.critical(f"Failed to load {path}, {e}")
        sys.exit(1)

    setup_logging(config["logging"].get("severity", "INFO"), str(log_path()))
    logger.info("Perplex — Discord Rich Presence for Plex")
    logger.info(f"Using {path}")

    try:
        Perplex(config).run()
    except KeyboardInterrupt:
        logger.info("Stopped")


def main() -> None:
    if is_frozen() or "--tray" in sys.argv[1:]:
        from .tray import main as tray_main

        tray_main()
    else:
        console()


if __name__ == "__main__":
    main()
