"""The Perplex engine: poll Plex, look up metadata, update Discord."""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal

from loguru import logger

from .metadata import TMDB
from .models import Metadata, NowPlaying
from .paths import resolve
from .plex import PlexWatcher
from .presence import DiscordPresence, build_activity

# Keep the presence through short gaps (e.g. autoplay moving to the next episode).
CLEAR_AFTER = 2  # consecutive empty polls

StatusKind = Literal["starting", "signin", "idle", "playing", "paused", "hidden", "waiting", "error"]


@dataclass(frozen=True)
class Status:
    kind: StatusKind
    text: str


def setup_logging(level: str, log_file: str | None = None) -> None:
    logger.remove()

    if sys.stderr is not None:  # None in the windowed .exe
        logger.add(sys.stderr, level=level.upper())

    if log_file:
        logger.add(log_file, level=level.upper(), rotation="1 MB", retention=2, encoding="utf-8")


class Perplex:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        on_status: Callable[[Status], Any] | None = None,
        open_url: Callable[[str], Any] | None = None,
        interactive: bool | None = None,
    ) -> None:
        self.config = config
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.hidden = False  # "Show my activity" turned off in the tray
        self.status = Status("starting", "Starting…")

        self.plex = PlexWatcher(config, open_url=open_url, interactive=interactive)
        self.discord = DiscordPresence(config["discord"]["appId"])
        self.tmdb: TMDB | None = None

        tmdb = config["tmdb"]
        if tmdb.get("enable") and tmdb.get("apiKey") and "XXXX" not in tmdb["apiKey"]:
            self.tmdb = TMDB(
                tmdb["apiKey"],
                language=tmdb.get("language", "en-US"),
                poster_size=tmdb.get("posterSize", "w500"),
                cache_file=resolve(tmdb.get("cacheFile")),
            )
        else:
            logger.warning("TMDB is disabled or has no API key; posters and director info won't be shown")

        # Per-run memo so a session costs zero lookups after its first poll.
        self._meta: dict[str, Metadata | None] = {}

    # ------------------------------------------------------------ status

    def _set_status(self, kind: StatusKind, text: str) -> None:
        status = Status(kind, text)

        if status != self.status:
            self.status = status
            if self.on_status:
                try:
                    self.on_status(status)
                except Exception as e:  # never let the UI break the loop
                    logger.debug(f"Status callback failed, {e}")

    # ---------------------------------------------------------- metadata

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

    # -------------------------------------------------------------- loop

    def tick(self, empty_polls: int) -> int:
        """Run one poll. Returns the updated count of consecutive empty polls."""

        if self.discord.problem:
            # Nothing can be shown until the settings are fixed; don't poll Plex for nothing.
            self._set_status("error", self.discord.problem)
            return empty_polls

        np = self.plex.now_playing()

        if np is None or (np.paused and self.config["discord"].get("clearOnPause")):
            empty_polls += 1
            if empty_polls >= CLEAR_AFTER or np is not None:
                self.discord.clear()
            if self.hidden:
                self._set_status("hidden", "Activity hidden")
            elif np is None:
                self._set_status("idle", "Nothing playing")
            else:
                self._set_status("paused", f"Paused: {np.show_title or np.title}")
            return empty_polls

        if self.hidden:
            self.discord.clear()
            self._set_status("hidden", "Activity hidden")
            return 0

        meta = self.metadata(np)
        activity = build_activity(np, meta, self.config, self.poster(np, meta))
        self.discord.show(activity)

        if self.discord.problem:
            self._set_status("error", self.discord.problem)
            return 0

        summary = " — ".join(t for t in (activity.details, activity.state) if t)
        if not self.discord.connected:
            self._set_status("waiting", f"Waiting for Discord · {summary}")
        else:
            self._set_status("paused" if np.paused else "playing", summary)

        return 0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        """Log in and poll until ``stop()`` is called."""

        self._set_status("signin", "Signing in to Plex…")
        self.plex.login()

        interval = max(float(self.config["plex"].get("pollInterval", 5)), 1.0)
        empty_polls = 0
        errors = 0
        logger.info(f"Watching Plex every {interval:g}s")
        self._set_status("idle", "Nothing playing")

        try:
            while not self.stop_event.is_set():
                try:
                    empty_polls = self.tick(empty_polls)
                    errors = 0
                except Exception as e:
                    errors += 1
                    logger.error(f"Plex poll failed, {e}")
                    self._set_status("error", f"Can't reach Plex: {e}"[:100])
                    empty_polls += 1
                    if empty_polls >= CLEAR_AFTER:
                        self.discord.clear()

                # Back off (up to ~1 minute) while Plex is unreachable.
                self.stop_event.wait(interval * min(2 ** max(errors - 1, 0), 12))
        finally:
            self.discord.close()
            if self.tmdb:
                self.tmdb.close()
