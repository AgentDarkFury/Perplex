import json
import urllib.parse
from datetime import datetime
from pathlib import Path
from sys import exit, stderr
from time import sleep
from typing import Any, Dict, List, Optional, Self, Union

import httpx
from httpx import Response
from loguru import logger
from plexapi.audio import TrackSession
from plexapi.media import Media
from plexapi.myplex import MyPlexAccount, MyPlexResource, PlexServer
from plexapi.video import EpisodeSession, MovieSession
from pypresence import ActivityType, Presence


class Perplex:
    """
    Discord Rich Presence implementation for Plex.

    https://github.com/EthanC/Perplex
    """

    def Initialize(self: Self) -> None:
        """Initialize Perplex and begin primary functionality."""

        logger.info("Perplex")
        logger.info("https://github.com/EthanC/Perplex")

        self.config: Dict[str, Any] = Perplex.LoadConfig(self)
        self.metadata_cache: Dict[str, Any] = {}

        Perplex.SetupLogging(self)

        plex: MyPlexAccount = Perplex.LoginPlex(self)
        discord: Presence = Perplex.LoginDiscord(self)

        while True:
            session: Optional[
                Union[MovieSession, EpisodeSession, TrackSession]
            ] = Perplex.FetchSession(self, plex)

            if session:
                logger.success(f"Fetched active media session")

                if type(session) is MovieSession:
                    status: Dict[str, Any] = Perplex.BuildMoviePresence(self, session)
                elif type(session) is EpisodeSession:
                    status: Dict[str, Any] = Perplex.BuildEpisodePresence(self, session)
                elif type(session) is TrackSession:
                    status: Dict[str, Any] = Perplex.BuildTrackPresence(self, session)

                success: bool = Perplex.SetPresence(self, discord, status)

                # Reestablish a failed Discord Rich Presence connection
                if not success:
                    discord = Perplex.LoginDiscord(self)
            else:
                try:
                    discord.clear()
                except Exception:
                    pass

            # Presence updates have a rate limit of 1 update per 15 seconds
            # https://discord.com/developers/docs/rich-presence/how-to#updating-presence
            logger.info("Sleeping for 15s...")

            sleep(15.0)

    def LoadConfig(self: Self) -> Dict[str, Any]:
        """Load the configuration values specified in config.json"""

        try:
            with open("config.json", "r") as file:
                config: Dict[str, Any] = json.loads(file.read())
        except Exception as e:
            logger.critical(f"Failed to load configuration, {e}")

            exit(1)

        logger.success("Loaded configuration")

        return config

    def SetupLogging(self: Self) -> None:
        """Setup the logger using the configured values."""

        settings: Dict[str, Any] = self.config["logging"]

        if (level := settings["severity"].upper()) != "DEBUG":
            try:
                logger.remove()
                logger.add(stderr, level=level)

                logger.success(f"Set logger severity to {level}")
            except Exception as e:
                # Fallback to default logger settings
                logger.add(stderr, level="DEBUG")

                logger.error(f"Failed to set logger severity to {level}, {e}")

    def LoginPlex(self: Self) -> MyPlexAccount:
        """Authenticate with Plex using the configured credentials."""

        settings: Dict[str, Any] = self.config["plex"]

        account: Optional[MyPlexAccount] = None

        if Path("auth.txt").is_file():
            try:
                with open("auth.txt", "r") as file:
                    auth: str = file.read()

                account = MyPlexAccount(token=auth)
            except Exception as e:
                logger.error(f"Failed to authenticate with Plex using token, {e}")

        if not account:
            username: str = settings["username"]
            password: str = settings["password"]

            if settings["twoFactor"]:
                print(f"Enter Verification Code: ", end="")
                code: str = input()

                if (code == "") or (code.isspace()):
                    logger.warning(
                        "Two-Factor Authentication is enabled but code was not supplied"
                    )
                else:
                    password = f"{password}{code}"

            try:
                account = MyPlexAccount(username, password)
            except Exception as e:
                logger.critical(f"Failed to authenticate with Plex, {e}")

                exit(1)

        logger.success("Authenticated with Plex")

        try:
            with open("auth.txt", "w+") as file:
                file.write(account.authenticationToken)
        except Exception as e:
            logger.error(
                f"Failed to save Plex authentication token for future logins, {e}"
            )

        return account

    def LoginDiscord(self: Self) -> Presence:
        """Authenticate with Discord using the configured credentials."""

        client: Optional[Presence] = None

        while not client:
            try:
                client = Presence(self.config["discord"]["appId"])
                client.connect()
            except Exception as e:
                logger.error(f"Failed to connect to Discord ({e}) retry in 15s...")

                sleep(15.0)

        logger.success("Authenticated with Discord")

        return client

    def FetchSession(
        self: Self, client: MyPlexAccount
    ) -> Optional[Union[MovieSession, EpisodeSession, TrackSession]]:
        """
        Connect to the configured Plex Media Server and return the active
        media session.
        """

        settings: Dict[str, Any] = self.config["plex"]

        resource: Optional[MyPlexResource] = None
        server: Optional[PlexServer] = None

        for entry in settings["servers"]:
            for result in client.resources():
                if entry.lower() == result.name.lower():
                    resource = result

                    break

            if resource:
                break

        if not resource:
            logger.critical("Failed to locate configured Plex Media Server")

            exit(1)

        try:
            server = resource.connect()
        except Exception as e:
            logger.critical(
                f"Failed to connect to configured Plex Media Server ({resource.name}), {e}"
            )

            exit(1)

        sessions: List[Media] = server.sessions()
        matching_sessions: List[Union[MovieSession, EpisodeSession, TrackSession]] = []

        configured_users = [entry.lower() for entry in settings.get("users", [])]

        for result in sessions:
            usernames = [alias.lower() for alias in getattr(result, "usernames", [])]
            if any(user in usernames for user in configured_users):
                matching_sessions.append(result)

        if not matching_sessions:
            logger.info("No active media sessions found for configured users")
            return

        def session_priority(s: Any) -> tuple[int, int]:
            player = getattr(s, "player", None) or (s.players[0] if getattr(s, "players", None) else None)
            state = str(getattr(player, "state", "")).lower() if player else ""
            state_score = 0
            if state == "playing":
                state_score = 3
            elif state == "buffering":
                state_score = 2
            elif state == "paused":
                state_score = 1

            session_key = getattr(s, "sessionKey", 0) or 0
            return (state_score, session_key)

        matching_sessions.sort(key=session_priority, reverse=True)
        active: Union[MovieSession, EpisodeSession, TrackSession] = matching_sessions[0]

        if type(active) is MovieSession:
            return active
        elif type(active) is EpisodeSession:
            return active
        elif type(active) is TrackSession:
            return active

        logger.error(f"Fetched active media session of unknown type: {type(active)}")

    def BuildMoviePresence(self: Self, active: MovieSession) -> Dict[str, Any]:
        """Build a Discord Rich Presence status for the active movie session."""

        minimal: bool = self.config["discord"]["minimal"]

        result: Dict[str, Any] = {}

        metadata: Optional[Dict[str, Any]] = Perplex.FetchMetadata(
            self, active.title, active.year, "movie", getattr(active, "guids", None)
        )

        if minimal:
            result["primary"] = active.title
        else:
            result["primary"] = f"{active.title} ({active.year})"

            details: List[str] = []

            if len(active.genres) > 0:
                details.append(active.genres[0].tag)

            if len(active.directors) > 0:
                details.append(f"Dir. {active.directors[0].tag}")

            if len(details) > 1:
                result["secondary"] = ", ".join(details)

        if not metadata:
            # Default to image uploaded via Discord Developer Portal
            result["image"] = "movie"
            result["buttons"] = []
        else:
            mId: int = metadata["id"]
            mType: str = metadata["media_type"]
            imgPath: str = metadata["poster_path"]

            result["image"] = f"https://image.tmdb.org/t/p/original{imgPath}"

            result["buttons"] = [
                {"label": "TMDB", "url": f"https://themoviedb.org/{mType}/{mId}"}
            ]

        now = int(datetime.now().timestamp())
        result["start"] = now - int(active.viewOffset / 1000)
        result["remaining"] = int((active.duration / 1000) - (active.viewOffset / 1000))
        result["imageText"] = active.title
        result["activity_type"] = ActivityType.WATCHING

        logger.trace(result)

        return result

    def BuildEpisodePresence(self: Self, active: EpisodeSession) -> Dict[str, Any]:
        """Build a Discord Rich Presence status for the active episode session."""

        result: Dict[str, Any] = {}

        metadata: Optional[Dict[str, Any]] = Perplex.FetchMetadata(
            self, active.show().title, active.show().year, "tv", getattr(active.show(), "guids", None)
        )

        result["primary"] = active.show().title
        result["secondary"] = active.title
        now = int(datetime.now().timestamp())
        result["start"] = now - int(active.viewOffset / 1000)
        result["remaining"] = int((active.duration / 1000) - (active.viewOffset / 1000))
        result["imageText"] = active.show().title
        result["activity_type"] = ActivityType.WATCHING

        if (active.seasonNumber) and (active.episodeNumber):
            result["secondary"] += f" (S{active.seasonNumber}:E{active.episodeNumber})"

        if not metadata:
            # Default to image uploaded via Discord Developer Portal
            result["image"] = "tv"
            result["buttons"] = []
        else:
            mId: int = metadata["id"]
            mType: str = metadata["media_type"]
            imgPath: str = metadata["poster_path"]

            result["image"] = f"https://image.tmdb.org/t/p/original{imgPath}"

            result["buttons"] = [
                {"label": "TMDB", "url": f"https://themoviedb.org/{mType}/{mId}"}
            ]

        logger.trace(result)

        return result

    def BuildTrackPresence(self: Self, active: TrackSession) -> Dict[str, Any]:
        """Build a Discord Rich Presence status for the active music session."""

        result: Dict[str, Any] = {}

        result["primary"] = active.titleSort
        now = int(datetime.now().timestamp())
        result["start"] = now - int(active.viewOffset / 1000)
        result["secondary"] = f"by {active.artist().title}"
        result["remaining"] = int((active.duration / 1000) - (active.viewOffset / 1000))
        result["imageText"] = active.parentTitle
        result["activity_type"] = ActivityType.LISTENING

        # Default to image uploaded via Discord Developer Portal
        result["image"] = "music"
        result["buttons"] = []

        logger.trace(result)

        return result

    def FetchMetadata(
        self: Self, title: str, year: int, format: str, guids: Optional[List[Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch metadata for the provided title/guids from TMDB."""

        settings: Dict[str, Any] = self.config["tmdb"]
        key: str = settings["apiKey"]

        if not settings["enable"]:
            logger.warning(f"TMDB disabled, some features will not be available")

            return

        if not hasattr(self, "metadata_cache"):
            self.metadata_cache = {}

        guid_key = ""
        if guids:
            guid_key = ",".join(sorted([getattr(g, "id", "") for g in guids if getattr(g, "id", "")]))
        cache_key = f"{format}:{title}:{year}:{guid_key}"

        if cache_key in self.metadata_cache:
            logger.trace(f"Using cached metadata for {title} ({year})")
            return self.metadata_cache[cache_key]

        tmdb_id = None
        imdb_id = None
        tvdb_id = None

        if guids:
            for guid_obj in guids:
                guid_str: str = getattr(guid_obj, "id", "")
                if guid_str.startswith("tmdb://"):
                    tmdb_id = guid_str.replace("tmdb://", "")
                elif guid_str.startswith("imdb://"):
                    imdb_id = guid_str.replace("imdb://", "")
                elif guid_str.startswith("tvdb://"):
                    tvdb_id = guid_str.replace("tvdb://", "")

        # 1. Try finding by direct TMDB ID
        if tmdb_id:
            try:
                res: Response = httpx.get(
                    f"https://api.themoviedb.org/3/{format}/{tmdb_id}?api_key={key}"
                )
                res.raise_for_status()
                data_tmdb: Dict[str, Any] = res.json()
                data_tmdb["media_type"] = format
                logger.debug(f"(HTTP {res.status_code}) GET {res.url}")
                self.metadata_cache[cache_key] = data_tmdb
                return data_tmdb
            except Exception as e:
                logger.warning(f"Failed to fetch metadata by TMDB ID {tmdb_id}, {e}")

        # 2. Try finding by IMDB or TVDB ID via find endpoint
        external_id = None
        external_source = None
        if imdb_id:
            external_id = imdb_id
            external_source = "imdb_id"
        elif tvdb_id:
            external_id = tvdb_id
            external_source = "tvdb_id"

        if external_id and external_source:
            try:
                res: Response = httpx.get(
                    f"https://api.themoviedb.org/3/find/{external_id}?external_source={external_source}&api_key={key}"
                )
                res.raise_for_status()
                data_ext: Dict[str, Any] = res.json()
                logger.debug(f"(HTTP {res.status_code}) GET {res.url}")
                
                results_key = "movie_results" if format == "movie" else "tv_results"
                results = data_ext.get(results_key, [])
                if results:
                    entry = results[0]
                    entry["media_type"] = format
                    self.metadata_cache[cache_key] = entry
                    return entry
            except Exception as e:
                logger.warning(f"Failed to fetch metadata by {external_source} {external_id}, {e}")

        # 3. Fallback to title/year search
        try:
            res: Response = httpx.get(
                f"https://api.themoviedb.org/3/search/multi?api_key={key}&query={urllib.parse.quote(title)}"
            )
            res.raise_for_status()

            logger.debug(f"(HTTP {res.status_code}) GET {res.url}")
            logger.trace(res.text)
        except Exception as e:
            logger.error(f"Failed to fetch metadata for {title} ({year}), {e}")
            self.metadata_cache[cache_key] = None
            return

        data: Dict[str, Any] = res.json()

        for entry in data.get("results", []):
            if format == "movie":
                if entry.get("media_type") != format:
                    continue
                elif title.lower() != entry.get("title", "").lower():
                    continue
                elif not entry.get("release_date", "").startswith(str(year)):
                    continue
            elif format == "tv":
                if entry.get("media_type") != format:
                    continue
                elif title.lower() != entry.get("name", "").lower():
                    continue
                elif not entry.get("first_air_date", "").startswith(str(year)):
                    continue

            self.metadata_cache[cache_key] = entry
            return entry

        logger.warning(f"Could not locate metadata for {title} ({year})")
        self.metadata_cache[cache_key] = None

    def SetPresence(self: Self, client: Presence, data: Dict[str, Any]) -> bool:
        """Set the Rich Presence status for the provided Discord client."""

        title: str = data["primary"]

        data["buttons"].append(
            {"label": "Get Perplex", "url": "https://github.com/EthanC/Perplex"}
        )

        try:
            client.update(
                details=title,
                state=data.get("secondary"),
                start=data.get("start"),
                end=int(datetime.now().timestamp() + data["remaining"]),
                large_image=data["image"],
                large_text=data["imageText"],
                small_image="plex",
                small_text="Plex",
                buttons=data["buttons"],
                activity_type=data.get("activity_type"),
                name=title,
            )
        except Exception as e:
            logger.error(f"Failed to set Discord Rich Presence to {title}, {e}")

            return False

        logger.success(f"Set Discord Rich Presence to {title}")

        return True


if __name__ == "__main__":
    try:
        Perplex.Initialize(Perplex)
    except KeyboardInterrupt:
        exit()
