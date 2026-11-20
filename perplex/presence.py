"""Build Discord activities from Plex sessions and push them without spamming Discord."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from loguru import logger
from pypresence import ActivityType, Presence, StatusDisplayType

from .models import Metadata, NowPlaying

# Plex only refreshes a session's position every few seconds, so a recomputed start
# time wobbles a little between polls. Only re-send when it moves more than this.
SEEK_TOLERANCE = 12
# Discord allows 5 activity updates per 20 seconds.
MIN_UPDATE_INTERVAL = 4.0
RECONNECT_INTERVAL = 15.0

_STATUS_DISPLAY = {
    "name": StatusDisplayType.NAME,
    "state": StatusDisplayType.STATE,
    "details": StatusDisplayType.DETAILS,
}

_FALLBACK_IMAGES = {"movie": "movie", "episode": "tv", "track": "music"}


@dataclass(frozen=True)
class Activity:
    activity_type: ActivityType
    status_display: StatusDisplayType
    details: str | None
    state: str | None
    large_image: str
    large_text: str | None
    small_image: str | None
    small_text: str | None
    details_url: str | None = None
    large_url: str | None = None
    buttons: tuple[tuple[str, str], ...] = ()
    start: int | None = None
    end: int | None = None

    def same_content(self, other: Activity | None) -> bool:
        """Equal ignoring timestamps."""

        if other is None:
            return False

        return replace(self, start=None, end=None) == replace(other, start=None, end=None)

    def same_timing(self, other: Activity) -> bool:
        if (self.start is None) != (other.start is None):
            return False
        if self.start is None or other.start is None:
            return True

        return abs(self.start - other.start) <= SEEK_TOLERANCE and abs(
            (self.end or 0) - self.start - ((other.end or 0) - other.start)
        ) <= 1


# ---------------------------------------------------------------- formatting


def format_runtime(minutes: int | None) -> str | None:
    if not minutes:
        return None

    hours, mins = divmod(int(minutes), 60)

    return f"{hours}h {mins}m" if hours and mins else f"{hours}h" if hours else f"{mins}m"


def render(template: str | list[str] | None, fields: dict[str, Any], sep: str = " · ") -> str:
    """
    Render a template made of one or more segments.

    A segment that references a missing/empty field is dropped entirely, so
    ["Dir. {director}", "{year}"] becomes just "2024" when the director is unknown.
    """

    if not template:
        return ""

    present = {k: v for k, v in fields.items() if v not in (None, "")}
    parts: list[str] = []

    for segment in [template] if isinstance(template, str) else template:
        try:
            text = segment.format_map(present).strip()
        except (KeyError, IndexError, ValueError, TypeError):
            continue

        if text:
            parts.append(text)

    return sep.join(parts)


def _fit(text: str | None, limit: int = 128) -> str | None:
    """Discord requires 2-128 characters for text fields."""

    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    if len(text) < 2:
        return text + "​"

    return text


def _fields(np: NowPlaying, meta: Metadata | None) -> dict[str, Any]:
    if np.kind == "movie":
        director = (meta.director if meta else None) or " & ".join(np.directors[:2]) or None
        runtime = (meta.runtime_min if meta else None) or (round(np.duration_ms / 60000) or None)

        return {
            "title": np.title or (meta.title if meta else None),
            "year": np.year or (meta.year if meta else None),
            "director": director,
            "runtime": format_runtime(runtime),
            "genre": np.genres[0] if np.genres else None,
        }

    if np.kind == "episode":
        return {
            "show": np.show_title or (meta.title if meta else None),
            "show_year": np.show_year or (meta.year if meta else None),
            "season": np.season,
            "episode": np.episode,
            "episode_title": np.title,
            "title": np.title,
            "runtime": format_runtime(round(np.duration_ms / 60000) or None),
        }

    return {"title": np.title, "artist": np.artist, "album": np.album}


def build_activity(
    np: NowPlaying,
    meta: Metadata | None,
    config: dict[str, Any],
    poster_url: str | None = None,
    now: float | None = None,
) -> Activity:
    settings = config["discord"]
    fmt = settings["formats"][np.kind]
    fields = _fields(np, meta)

    details = render(fmt.get("details"), fields)
    state = render(fmt.get("state"), fields)
    large_text = render(fmt.get("largeText"), fields) or details

    start = end = None
    if not np.paused and np.duration_ms > 0:
        now = time.time() if now is None else now
        start = int(now - np.offset_ms / 1000)
        end = start + int(np.duration_ms / 1000)

    buttons: list[tuple[str, str]] = []
    if meta and settings.get("buttons", True):
        buttons.append(("View on TMDB", meta.url))
        if meta.imdb_url:
            buttons.append(("View on IMDb", meta.imdb_url))

    return Activity(
        activity_type=ActivityType.LISTENING if np.kind == "track" else ActivityType.WATCHING,
        status_display=_STATUS_DISPLAY.get(str(settings.get("statusDisplay")).lower(), StatusDisplayType.DETAILS),
        details=_fit(details),
        state=_fit(state),
        large_image=poster_url or _FALLBACK_IMAGES[np.kind],
        large_text=_fit(large_text),
        small_image=settings.get("smallImage") or None,
        small_text="Paused" if np.paused else "Plex",
        details_url=meta.url if meta else None,
        large_url=meta.url if meta else None,
        buttons=tuple(buttons[:2]),
        start=start,
        end=end,
    )


# ------------------------------------------------------------------ discord


class DiscordPresence:
    """A Discord RPC connection that only sends updates when something changed."""

    def __init__(self, app_id: str | int) -> None:
        self.app_id = str(app_id)
        self.rpc: Presence | None = None
        self.current: Activity | None = None
        self._next_connect = 0.0
        self._last_update = 0.0

    def _connected(self) -> bool:
        if self.rpc:
            return True
        if time.monotonic() < self._next_connect:
            return False

        try:
            rpc = Presence(self.app_id)
            rpc.connect()
        except Exception as e:
            self._next_connect = time.monotonic() + RECONNECT_INTERVAL
            logger.warning(f"Discord isn't reachable ({e}), retrying in {RECONNECT_INTERVAL:.0f}s")
            return False

        self.rpc = rpc
        self.current = None
        logger.success("Connected to Discord")

        return True

    def _drop(self, error: Exception) -> None:
        logger.error(f"Lost Discord connection, {error}")

        try:
            if self.rpc:
                self.rpc.close()
        except Exception:
            pass

        self.rpc = None
        self.current = None
        self._next_connect = time.monotonic() + RECONNECT_INTERVAL

    def show(self, activity: Activity) -> bool:
        """Push ``activity`` if it differs from what Discord shows. Returns True if sent."""

        if activity.same_content(self.current) and activity.same_timing(self.current):  # type: ignore[arg-type]
            return False
        if time.monotonic() - self._last_update < MIN_UPDATE_INTERVAL:
            return False  # The next poll will retry
        if not self._connected():
            return False

        assert self.rpc
        try:
            self.rpc.update(
                activity_type=activity.activity_type,
                status_display_type=activity.status_display,
                details=activity.details,
                details_url=activity.details_url,
                state=activity.state,
                start=activity.start,
                end=activity.end,
                large_image=activity.large_image,
                large_text=activity.large_text,
                large_url=activity.large_url,
                small_image=activity.small_image,
                small_text=activity.small_text,
                buttons=[{"label": label, "url": url} for label, url in activity.buttons] or None,
            )
        except Exception as e:
            self._drop(e)
            return False

        self._last_update = time.monotonic()
        moved = self.current is not None and activity.same_content(self.current)
        self.current = activity
        logger.success(
            f"{'Resynced progress for' if moved else 'Set Discord presence to'} "
            f"{activity.details}{f' — {activity.state}' if activity.state else ''}"
        )

        return True

    def clear(self) -> None:
        if self.current is None or not self.rpc:
            return

        try:
            self.rpc.clear()
            logger.info("Cleared Discord presence")
        except Exception as e:
            self._drop(e)

        self.current = None

    def close(self) -> None:
        try:
            self.clear()
            if self.rpc:
                self.rpc.close()
        except Exception:
            pass
