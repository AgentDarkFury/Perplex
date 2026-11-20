"""Configuration loading with defaults so older config.json files keep working."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "logging": {"severity": "INFO"},
    "plex": {
        # Optional direct server URL (e.g. "http://192.168.1.10:32400"). Skips the
        # plex.tv resource lookup, which is faster and works when plex.tv is down.
        "url": "",
        "username": "",
        "password": "",
        "twoFactor": False,
        "servers": [],
        "users": [],
        # Seconds between Plex polls. Discord is only updated when something changes,
        # so a short interval is cheap and keeps the progress bar accurate after seeks.
        "pollInterval": 5,
    },
    "tmdb": {
        "enable": True,
        # Either a v3 API key or a v4 "API Read Access Token" (starts with "eyJ").
        "apiKey": "",
        "language": "en-US",
        "posterSize": "w500",
        # Use the season's poster for episodes instead of the show poster.
        "seasonPosters": False,
        "cacheFile": "cache.json",
    },
    "discord": {
        "appId": "923857760897101855",
        # What appears after "Watching" in the member list: "details", "state" or "name".
        "statusDisplay": "details",
        "clearOnPause": False,
        "buttons": True,
        "smallImage": "plex",
        "formats": {
            "movie": {
                "details": "{title}",
                "state": ["Dir. {director}", "{year}", "{runtime}"],
                "largeText": "{title} ({year})",
            },
            "episode": {
                "details": "{show}",
                "state": ["S{season:02}E{episode:02}", "{episode_title}"],
                "largeText": ["Season {season}", "Episode {episode}"],
            },
            "track": {
                "details": "{title}",
                "state": "by {artist}",
                "largeText": "{album}",
            },
        },
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""

    result = copy.deepcopy(base)

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value

    return result


def load_config(path: str | Path = "config.json") -> dict[str, Any]:
    """Load config.json and fill in any missing values with defaults."""

    with open(path, "r", encoding="utf-8") as file:
        user_config: dict[str, Any] = json.load(file)

    config = _merge(DEFAULTS, user_config)

    # Environment overrides keep secrets out of config.json if preferred.
    if key := os.environ.get("TMDB_API_KEY"):
        config["tmdb"]["apiKey"] = key

    return config


# What a brand-new config.json looks like (mirrors config_example.json).
STARTER: dict[str, Any] = {
    "logging": {"severity": "INFO"},
    "plex": {
        "url": "",
        "username": "",
        "password": "",
        "twoFactor": False,
        "servers": [],
        "users": [],
        "pollInterval": 5,
    },
    "tmdb": {
        "enable": True,
        "apiKey": "",
        "language": "en-US",
        "posterSize": "w500",
        "seasonPosters": False,
    },
    "discord": {
        "appId": DEFAULTS["discord"]["appId"],
        "statusDisplay": "details",
        "clearOnPause": False,
        "buttons": True,
    },
}


def write_starter_config(path: str | Path) -> None:
    """Create a starter config.json if none exists."""

    path = Path(path)

    if not path.exists():
        path.write_text(json.dumps(STARTER, indent=4) + "\n", encoding="utf-8")
