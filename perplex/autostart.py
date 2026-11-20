"""'Start with Windows' via the current user's Run registry key (no admin needed)."""

from __future__ import annotations

import sys

from loguru import logger

from .paths import is_frozen

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Perplex"


def supported() -> bool:
    # Only the packaged .exe has a stable path worth registering.
    return sys.platform == "win32" and is_frozen()


def command() -> str:
    return f'"{sys.executable}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False

    return str(value).strip().lower() == command().lower()


def set_enabled(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass

    logger.info(f"Start with Windows {'enabled' if enabled else 'disabled'}")
