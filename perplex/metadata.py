"""
TMDB metadata lookup.

Resolution order, cheapest and most accurate first:
  1. A TMDB ID from Plex's GUIDs           -> 1 request (details)
  2. An IMDb/TVDB ID via /find              -> 2 requests (find + details)
  3. A title/year search                    -> 2 requests (search + details)

Details are fetched with ``append_to_response`` so director, runtime, IMDb ID and
season posters all arrive in one response. Results go into a small JSON cache on
disk, so an item already seen costs no requests at all, even after a restart.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Literal

import httpx
from loguru import logger

from .models import Metadata

MediaType = Literal["movie", "tv"]

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

DETAILS_TTL = 7 * 24 * 3600  # Refresh details weekly (posters/runtime rarely change)
LOOKUP_TTL = 30 * 24 * 3600  # Plex GUID/title -> TMDB ID mappings are stable
MISS_TTL = 6 * 3600  # Retry unmatched titles a few times a day
ERROR_BACKOFF = 120  # Don't hammer TMDB while it (or the network) is failing

_GUID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tmdb", re.compile(r"^(?:tmdb|com\.plexapp\.agents\.themoviedb)://(\d+)")),
    ("imdb", re.compile(r"^(?:imdb|com\.plexapp\.agents\.imdb)://(tt\d+)")),
    ("tvdb", re.compile(r"^(?:tvdb|com\.plexapp\.agents\.thetvdb)://(\d+)")),
)


def parse_guids(guids: Iterable[str]) -> dict[str, str]:
    """Extract {"tmdb": ..., "imdb": ..., "tvdb": ...} from Plex GUID strings."""

    ids: dict[str, str] = {}

    for guid in guids:
        for source, pattern in _GUID_PATTERNS:
            if source not in ids and (match := pattern.match(guid or "")):
                ids[source] = match.group(1)

    return ids


def normalize_title(title: str) -> str:
    """Casefold, strip accents and punctuation so "Amélie" == "amelie"."""

    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", "and")

    return re.sub(r"[^a-z0-9]+", "", text)


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


class _DiskCache:
    """A tiny JSON key/value store with per-entry expiry."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        self.dirty = False
        self.lock = threading.Lock()

        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self._prune()
                logger.debug(f"Loaded {len(self.data)} cached metadata entries")
            except Exception as e:
                logger.warning(f"Ignoring unreadable metadata cache {path}, {e}")

    def _prune(self) -> None:
        now = time.time()
        expired = [k for k, v in self.data.items() if v.get("expires", 0) < now]

        for key in expired:
            del self.data[key]

        self.dirty = self.dirty or bool(expired)

    def get(self, key: str) -> tuple[bool, Any]:
        entry = self.data.get(key)

        if entry is None or entry.get("expires", 0) < time.time():
            return False, None

        return True, entry.get("value")

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self.lock:
            self.data[key] = {"value": value, "expires": time.time() + ttl}
            self.dirty = True

    def save(self) -> None:
        if not (self.path and self.dirty):
            return

        with self.lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")

            try:
                tmp.write_text(json.dumps(self.data, separators=(",", ":")), "utf-8")
                os.replace(tmp, self.path)
                self.dirty = False
            except Exception as e:
                logger.warning(f"Failed to write metadata cache {self.path}, {e}")


class TMDB:
    """Minimal, cached TMDB client."""

    def __init__(
        self,
        api_key: str,
        *,
        language: str = "en-US",
        poster_size: str = "w500",
        cache_file: str | Path | None = "cache.json",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        params: dict[str, str] = {"language": language}

        # v4 read access tokens are JWTs; v3 keys are 32 hex characters.
        if api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            params["api_key"] = api_key

        self.http = httpx.Client(
            base_url=API_BASE,
            headers=headers,
            params=params,
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
        )
        self.poster_size = poster_size
        self.cache = _DiskCache(Path(cache_file) if cache_file else None)
        self._failed_until = 0.0

    # ------------------------------------------------------------------ public

    def lookup(
        self,
        media_type: MediaType,
        title: str,
        year: int | None,
        guids: Iterable[str] = (),
    ) -> Metadata | None:
        """Return metadata for a movie or TV show, or None if it can't be found."""

        guids = tuple(guids)
        ids = parse_guids(guids)
        lookup_key = self._lookup_key(media_type, title, year, ids)

        tmdb_id: int | None
        if ids.get("tmdb"):
            tmdb_id = int(ids["tmdb"])
        else:
            hit, tmdb_id = self.cache.get(lookup_key)

            if not hit:
                if self._backing_off():
                    return None

                try:
                    tmdb_id = self._resolve(media_type, title, year, ids)
                except httpx.HTTPError as e:
                    self._fail(f"resolve {media_type} '{title}'", e)
                    return None

                self.cache.set(lookup_key, tmdb_id, LOOKUP_TTL if tmdb_id else MISS_TTL)

                if not tmdb_id:
                    logger.warning(f"No TMDB match for {media_type} '{title}' ({year})")

        if not tmdb_id:
            self.cache.save()
            return None

        metadata = self._details(media_type, tmdb_id)
        self.cache.save()

        return metadata

    def poster_url(self, path: str | None) -> str | None:
        return f"{IMAGE_BASE}/{self.poster_size}{path}" if path else None

    def close(self) -> None:
        self.cache.save()
        self.http.close()

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _lookup_key(
        media_type: MediaType, title: str, year: int | None, ids: dict[str, str]
    ) -> str:
        if ids.get("imdb"):
            return f"id:{media_type}:imdb:{ids['imdb']}"
        if ids.get("tvdb"):
            return f"id:{media_type}:tvdb:{ids['tvdb']}"

        return f"id:{media_type}:title:{normalize_title(title)}:{year or ''}"

    def _backing_off(self) -> bool:
        return time.time() < self._failed_until

    def _fail(self, action: str, error: Exception) -> None:
        self._failed_until = time.time() + ERROR_BACKOFF
        logger.error(f"TMDB request failed ({action}), retrying in {ERROR_BACKOFF}s: {error}")

    def _get(self, path: str, **params: Any) -> dict[str, Any] | None:
        """GET a TMDB endpoint. Returns None on 404, raises on other failures."""

        for attempt in range(3):
            res = self.http.get(path, params={k: v for k, v in params.items() if v})
            logger.debug(f"(HTTP {res.status_code}) GET {path}")

            if res.status_code == 404:
                return None

            if res.status_code == 429 or res.status_code >= 500:
                if attempt < 2:
                    retry_after = res.headers.get("Retry-After", "")
                    time.sleep(min(float(retry_after) if retry_after.isdigit() else 1.0 + attempt, 10))
                    continue

            res.raise_for_status()

            return res.json()

        return None  # pragma: no cover - loop always returns or raises

    def _resolve(
        self, media_type: MediaType, title: str, year: int | None, ids: dict[str, str]
    ) -> int | None:
        """Turn IMDb/TVDB IDs or a title+year into a TMDB ID."""

        results_key = "movie_results" if media_type == "movie" else "tv_results"

        for source in ("imdb", "tvdb"):
            if not ids.get(source):
                continue

            data = self._get(f"/find/{ids[source]}", external_source=f"{source}_id")

            if data and (results := data.get(results_key)):
                return int(results[0]["id"])

        if not title:
            return None

        return self._search(media_type, title, year)

    def _search(self, media_type: MediaType, title: str, year: int | None) -> int | None:
        year_param = "year" if media_type == "movie" else "first_air_date_year"

        results: list[dict[str, Any]] = []
        if year:
            results = (self._get(f"/search/{media_type}", query=title, **{year_param: year}) or {}).get("results", [])
        if not results:
            # Plex and TMDB sometimes disagree on the year by one, so search wide.
            results = (self._get(f"/search/{media_type}", query=title) or {}).get("results", [])

        return self._best_match(media_type, title, year, results)

    @staticmethod
    def _best_match(
        media_type: MediaType, title: str, year: int | None, results: list[dict[str, Any]]
    ) -> int | None:
        wanted = normalize_title(title)
        date_key = "release_date" if media_type == "movie" else "first_air_date"
        title_keys = ("title", "original_title") if media_type == "movie" else ("name", "original_name")

        best: tuple[float, int] | None = None

        for rank, entry in enumerate(results[:20]):
            names = {normalize_title(entry.get(k) or "") for k in title_keys}
            entry_year = _year(entry.get(date_key))

            score = 0.0
            if wanted in names:
                score += 10
            elif any(wanted and n and (wanted in n or n in wanted) for n in names):
                score += 3
            else:
                continue

            if year and entry_year:
                diff = abs(year - entry_year)
                score += 5 if diff == 0 else 2 if diff == 1 else -5

            score -= rank * 0.1  # TMDB orders by relevance; keep that as a tiebreak

            if best is None or score > best[0]:
                best = (score, int(entry["id"]))

        return best[1] if best and best[0] > 0 else None

    def _details(self, media_type: MediaType, tmdb_id: int) -> Metadata | None:
        key = f"{media_type}:{tmdb_id}"
        hit, cached = self.cache.get(key)

        if hit:
            return Metadata(**cached) if cached else None

        if self._backing_off():
            return None

        append = "credits,external_ids" if media_type == "movie" else "external_ids"

        try:
            data = self._get(f"/{media_type}/{tmdb_id}", append_to_response=append)
        except httpx.HTTPError as e:
            self._fail(f"{media_type} {tmdb_id}", e)
            return None

        if not data:
            self.cache.set(key, None, MISS_TTL)
            return None

        metadata = self._parse_details(media_type, tmdb_id, data)
        self.cache.set(key, asdict(metadata), DETAILS_TTL)
        logger.success(f"Fetched TMDB metadata for {metadata.title} ({metadata.year})")

        return metadata

    @staticmethod
    def _parse_details(media_type: MediaType, tmdb_id: int, data: dict[str, Any]) -> Metadata:
        external = data.get("external_ids") or {}

        if media_type == "movie":
            crew = (data.get("credits") or {}).get("crew") or []
            directors = [c["name"] for c in crew if c.get("job") == "Director" and c.get("name")]

            return Metadata(
                media_type="movie",
                tmdb_id=tmdb_id,
                title=data.get("title") or data.get("original_title") or "",
                year=_year(data.get("release_date")),
                poster_path=data.get("poster_path"),
                director=" & ".join(directors[:2]) or None,
                runtime_min=data.get("runtime") or None,
                imdb_id=data.get("imdb_id") or external.get("imdb_id"),
            )

        seasons = {
            str(s["season_number"]): s["poster_path"]
            for s in data.get("seasons") or []
            if s.get("poster_path") and s.get("season_number") is not None
        }
        runtimes = data.get("episode_run_time") or []

        return Metadata(
            media_type="tv",
            tmdb_id=tmdb_id,
            title=data.get("name") or data.get("original_name") or "",
            year=_year(data.get("first_air_date")),
            poster_path=data.get("poster_path"),
            season_posters=seasons,
            runtime_min=runtimes[0] if runtimes else None,
            imdb_id=external.get("imdb_id"),
        )
