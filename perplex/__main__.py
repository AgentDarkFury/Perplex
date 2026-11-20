"""Entry point: ``python -m perplex``."""

from __future__ import annotations

import sys
import time
from typing import Any

from loguru import logger

from .config import load_config
from .metadata import TMDB
from .models import Metadata, NowPlaying
from .plex import PlexWatcher
from .presence import DiscordPresence, build_activity

# Keep the presence through short gaps (e.g. autoplay moving to the next episode).
CLEAR_AFTER = 2  # consecutive empty polls


def setup_logging(level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper())


class Perplex:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.plex = PlexWatcher(config)
        self.discord = DiscordPresence(config["discord"]["appId"])
        self.tmdb: TMDB | None = None

        tmdb = config["tmdb"]
        if tmdb.get("enable") and tmdb.get("apiKey") and "XXXX" not in tmdb["apiKey"]:
            self.tmdb = TMDB(
                tmdb["apiKey"],
                language=tmdb.get("language", "en-US"),
                poster_size=tmdb.get("posterSize", "w500"),
                cache_file=tmdb.get("cacheFile") or None,
            )
        else:
            logger.warning("TMDB is disabled or has no API key; posters and director info won't be shown")

        # Per-run memo so a session costs zero lookups after its first poll.
        self._meta: dict[str, Metadata | None] = {}

    def metadata(self, np: NowPlaying) -> Metadata | None:
        if not self.tmdb or np.kind == "track":
            return None

        if np.kind == "movie":
            key = f"movie:{np.rating_key}"
            if key not in self._meta:
                self._meta[key] = self.tmdb.lookup("movie", np.title, np.year, np.guids)
        else:
            key = f"show:{np.show_rating_key}"
            if key not in self._meta:
                self._meta[key] = self.tmdb.lookup("tv", np.show_title or "", np.show_year, np.show_guids)

        return self._meta[key]

    def poster(self, np: NowPlaying, meta: Metadata | None) -> str | None:
        if not meta or not self.tmdb:
            return None

        path = meta.poster_path
        if np.kind == "episode" and self.config["tmdb"].get("seasonPosters") and np.season is not None:
            path = meta.season_posters.get(str(np.season)) or path

        return self.tmdb.poster_url(path)

    def tick(self, empty_polls: int) -> int:
        """Run one poll. Returns the updated count of consecutive empty polls."""

        np = self.plex.now_playing()

        if np is None or (np.paused and self.config["discord"].get("clearOnPause")):
            empty_polls += 1
            if empty_polls >= CLEAR_AFTER or np is not None:
                self.discord.clear()
            return empty_polls

        meta = self.metadata(np)
        self.discord.show(build_activity(np, meta, self.config, self.poster(np, meta)))

        return 0

    def run(self) -> None:
        self.plex.login()
        interval = max(float(self.config["plex"].get("pollInterval", 5)), 1.0)
        empty_polls = 0

        logger.info(f"Watching Plex every {interval:g}s")

        try:
            while True:
                try:
                    empty_polls = self.tick(empty_polls)
                except Exception as e:
                    logger.error(f"Plex poll failed, {e}")
                    empty_polls += 1
                    if empty_polls >= CLEAR_AFTER:
                        self.discord.clear()

                time.sleep(interval)
        finally:
            self.discord.close()
            if self.tmdb:
                self.tmdb.close()


def main() -> None:
    try:
        config = load_config()
    except Exception as e:
        logger.critical(f"Failed to load config.json, {e}")
        sys.exit(1)

    setup_logging(config["logging"].get("severity", "INFO"))
    logger.info("Perplex — Discord Rich Presence for Plex")

    try:
        Perplex(config).run()
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
