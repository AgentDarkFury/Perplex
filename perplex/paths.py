"""Where Perplex keeps config.json, auth.txt, cache.json and its log."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running as the packaged Perplex.exe."""

    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    """
    The folder holding Perplex's files.

    - ``PERPLEX_HOME`` if set.
    - The current folder when running from source with a config.json there
      (keeps the original "run it from the repo" workflow working).
    - Otherwise ``%APPDATA%\\Perplex``.
    """

    if home := os.environ.get("PERPLEX_HOME"):
        path = Path(home)
    elif not is_frozen() and Path("config.json").is_file():
        path = Path.cwd()
    else:
        base = os.environ.get("APPDATA") or str(Path.home() / ".config")
        path = Path(base) / "Perplex"

    path.mkdir(parents=True, exist_ok=True)

    return path


def config_path() -> Path:
    return data_dir() / "config.json"


def token_path() -> Path:
    return data_dir() / "auth.txt"


def log_path() -> Path:
    return data_dir() / "perplex.log"


def resolve(name: str | None) -> Path | None:
    """Resolve a (possibly relative) file name from config against the data folder."""

    if not name:
        return None

    path = Path(name)

    return path if path.is_absolute() else data_dir() / path
