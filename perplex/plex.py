"""Plex login, server connection, and active-session snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from plexapi.myplex import MyPlexAccount, MyPlexPinLogin
from plexapi.server import PlexServer

from .models import NowPlaying

TOKEN_FILE = Path("auth.txt")

_STATE_PRIORITY = {"playing": 3, "buffering": 2, "paused": 1}


def _guid_ids(item: Any) -> tuple[str, ...]:
    """All GUIDs Plex knows for an item, including legacy-agent ``guid`` strings."""

    ids = [g.id for g in (getattr(item, "guids", None) or []) if getattr(g, "id", None)]

    if (guid := getattr(item, "guid", None)) and guid not in ids:
        ids.append(guid)

    return tuple(ids)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class PlexWatcher:
    """Owns the Plex connection and turns sessions into ``NowPlaying`` snapshots."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.settings = config["plex"]
        self.account: MyPlexAccount | None = None
        self.server: PlexServer | None = None
        self._show_cache: dict[str, tuple[int | None, tuple[str, ...]]] = {}
        self._guid_cache: dict[str, tuple[str, ...]] = {}

    # -------------------------------------------------------------- login

    def login(self) -> MyPlexAccount:
        account: MyPlexAccount | None = None

        if TOKEN_FILE.is_file():
            try:
                account = MyPlexAccount(token=TOKEN_FILE.read_text().strip())
            except Exception as e:
                logger.warning(f"Saved Plex token was rejected, logging in again ({e})")

        if not account and self.settings.get("username") and self.settings.get("password"):
            password: str = self.settings["password"]

            if self.settings.get("twoFactor"):
                code = input("Enter Plex verification code: ").strip()
                password += code

            try:
                account = MyPlexAccount(self.settings["username"], password)
            except Exception as e:
                logger.error(f"Plex username/password login failed, {e}")

        if not account:
            # No password needed: approve this device in the browser instead.
            pin = MyPlexPinLogin(oauth=True)
            pin.run(timeout=300)
            print(f"\nSign in to Plex to link Perplex:\n  {pin.oauthUrl()}\n")
            pin.waitForLogin()

            if not pin.token:
                raise SystemExit("Plex sign-in was not completed")

            account = MyPlexAccount(token=pin.token)

        try:
            TOKEN_FILE.write_text(account.authenticationToken)
        except Exception as e:
            logger.warning(f"Failed to save Plex token for future logins, {e}")

        logger.success(f"Authenticated with Plex as {account.username}")
        self.account = account

        return account

    # ------------------------------------------------------------ server

    def _connect(self) -> PlexServer:
        if self.server:
            return self.server

        assert self.account, "login() must be called first"

        if url := self.settings.get("url"):
            self.server = PlexServer(url, self.account.authenticationToken, timeout=10)
        else:
            wanted = [name.lower() for name in self.settings.get("servers", [])]
            resources = [r for r in self.account.resources() if "server" in (r.provides or "")]

            if wanted:
                resources.sort(
                    key=lambda r: wanted.index(r.name.lower()) if r.name.lower() in wanted else len(wanted)
                )
                resources = [r for r in resources if r.name.lower() in wanted]

            if not resources:
                raise ConnectionError("None of the configured Plex servers were found on this account")

            errors: list[str] = []
            for resource in resources:
                try:
                    self.server = resource.connect(timeout=10)
                    break
                except Exception as e:
                    errors.append(f"{resource.name}: {e}")

            if not self.server:
                raise ConnectionError("Could not reach any Plex server; " + "; ".join(errors))

        logger.success(f"Connected to Plex Media Server {self.server.friendlyName}")

        return self.server

    def disconnect(self) -> None:
        self.server = None

    # ---------------------------------------------------------- sessions

    def _wanted_users(self) -> list[str]:
        users = [u.lower() for u in self.settings.get("users", []) if u]

        if not users and self.account:
            users = [n.lower() for n in (self.account.username, self.account.title) if n]

        return users

    def now_playing(self) -> NowPlaying | None:
        """Return the highest-priority session for the configured users, if any."""

        server = self._connect()

        try:
            sessions = server.sessions()
        except Exception:
            self.disconnect()  # Reconnect (possibly to another server) next poll
            raise

        users = self._wanted_users()
        candidates: list[tuple[tuple[int, int], Any]] = []

        for session in sessions:
            if getattr(session, "TYPE", None) not in ("movie", "episode", "track"):
                continue

            names = [n.lower() for n in getattr(session, "usernames", []) if n]
            matches = [users.index(n) for n in names if n in users]

            if not matches:
                continue

            state = (getattr(session.player, "state", "") or "").lower() if session.player else ""
            # Prefer actively playing sessions, then higher-priority users.
            candidates.append(((_STATE_PRIORITY.get(state, 0), -min(matches)), session))

        if not candidates:
            return None

        candidates.sort(key=lambda c: c[0], reverse=True)

        return self._snapshot(candidates[0][1])

    def _snapshot(self, session: Any) -> NowPlaying:
        kind = session.TYPE
        state = (getattr(session.player, "state", "") or "playing").lower() if session.player else "playing"
        common = dict(
            kind=kind,
            rating_key=str(session.ratingKey),
            title=session.title or "",
            duration_ms=int(session.duration or 0),
            offset_ms=int(session.viewOffset or 0),
            state=state,
            year=_int(getattr(session, "year", None)),
        )

        if kind == "movie":
            return NowPlaying(
                **common,
                guids=self._item_guids(session),
                directors=tuple(d.tag for d in getattr(session, "directors", []) or []),
                genres=tuple(g.tag for g in getattr(session, "genres", []) or []),
            )

        if kind == "episode":
            show_key = str(session.grandparentRatingKey)
            show_year, show_guids = self._show_info(show_key)

            return NowPlaying(
                **common,
                show_title=session.grandparentTitle,
                show_rating_key=show_key,
                show_year=show_year,
                show_guids=show_guids,
                season=_int(session.parentIndex),
                episode=_int(session.index),
            )

        return NowPlaying(
            **common,
            artist=getattr(session, "originalTitle", None) or session.grandparentTitle,
            album=session.parentTitle,
        )

    def _item_guids(self, session: Any) -> tuple[str, ...]:
        """Session payloads sometimes omit GUIDs; fetch the full item once if so."""

        key = str(session.ratingKey)

        if key not in self._guid_cache:
            guids = _guid_ids(session)

            if not any(g.startswith(("tmdb://", "imdb://", "tvdb://")) for g in guids):
                try:
                    guids = _guid_ids(self._connect().fetchItem(int(key))) or guids
                except Exception as e:
                    logger.debug(f"Could not load GUIDs for item {key}, {e}")

            self._guid_cache[key] = guids

        return self._guid_cache[key]

    def _show_info(self, show_key: str) -> tuple[int | None, tuple[str, ...]]:
        """Year and GUIDs for a show, fetched once per show per run."""

        if show_key not in self._show_cache:
            try:
                show = self._connect().fetchItem(int(show_key))
                self._show_cache[show_key] = (_int(show.year), _guid_ids(show))
            except Exception as e:
                logger.warning(f"Could not load show {show_key} from Plex, {e}")
                return None, ()

        return self._show_cache[show_key]
