"""Plain data objects passed between the Plex, metadata, and Discord layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["movie", "episode", "track"]


@dataclass(frozen=True)
class NowPlaying:
    """A snapshot of the active Plex session, detached from plexapi objects."""

    kind: Kind
    rating_key: str
    title: str
    duration_ms: int
    offset_ms: int
    state: str = "playing"
    year: int | None = None
    guids: tuple[str, ...] = ()
    directors: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()

    # Episodes
    show_title: str | None = None
    show_rating_key: str | None = None
    show_year: int | None = None
    show_guids: tuple[str, ...] = ()
    season: int | None = None
    episode: int | None = None

    # Tracks
    artist: str | None = None
    album: str | None = None

    @property
    def paused(self) -> bool:
        return self.state == "paused"


@dataclass
class Metadata:
    """The subset of TMDB data Perplex needs. Serialisable to the disk cache."""

    media_type: Literal["movie", "tv"]
    tmdb_id: int
    title: str
    year: int | None = None
    poster_path: str | None = None
    season_posters: dict[str, str] = field(default_factory=dict)
    director: str | None = None
    runtime_min: int | None = None
    imdb_id: str | None = None

    @property
    def url(self) -> str:
        return f"https://www.themoviedb.org/{self.media_type}/{self.tmdb_id}"

    @property
    def imdb_url(self) -> str | None:
        return f"https://www.imdb.com/title/{self.imdb_id}/" if self.imdb_id else None
