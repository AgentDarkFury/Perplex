"""System tray app: runs the Perplex engine in the background with a small menu."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import pystray
from loguru import logger

from . import autostart
from .app import Perplex, Status, setup_logging
from .config import load_config, write_starter_config
from .icon import make_icon
from .paths import config_path, log_path

CONFIG_POLL = 2.0  # seconds between config.json change checks
RETRY_AFTER_ERROR = 60.0

_mutex = None  # keeps the single-instance mutex alive for the process lifetime


def _single_instance() -> bool:
    """False if another Perplex tray app is already running for this user."""

    global _mutex

    if sys.platform != "win32":
        return True

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, "Local\\Perplex-Tray")

    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _message_box(text: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Perplex", 0x40)  # MB_ICONINFORMATION
    else:
        print(text)


def _open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(path)])


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class TrayApp:
    def __init__(self) -> None:
        self.config_file = config_path()
        self.status = Status("starting", "Starting…")
        self.hidden = False
        self.engine: Perplex | None = None
        self.quitting = threading.Event()
        self.reload = threading.Event()
        self.first_run = not self.config_file.exists()

        self.icon = pystray.Icon(
            "Perplex",
            icon=make_icon("starting"),
            title="Perplex — Starting…",
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: self.status.text[:64], None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Show my activity", self._toggle_hidden, checked=lambda _: not self.hidden),
                pystray.MenuItem("Settings…", self._open_settings, default=True),
                pystray.MenuItem("Open log", self._open_log),
                pystray.MenuItem(
                    "Start with Windows",
                    self._toggle_autostart,
                    checked=lambda _: autostart.is_enabled(),
                    enabled=lambda _: autostart.supported(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    # ------------------------------------------------------------ status

    def _on_status(self, status: Status) -> None:
        self.status = status
        self.icon.icon = make_icon(status.kind)
        self.icon.title = f"Perplex — {status.text}"[:127]  # Windows tooltip limit
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "Perplex")
        except Exception as e:
            logger.debug(f"Notification failed, {e}")

    def _open_url(self, url: str) -> None:
        self._notify("Sign in to Plex in your browser to link Perplex.")
        webbrowser.open(url)

    # ------------------------------------------------------------- menu

    def _toggle_hidden(self, *_: object) -> None:
        self.hidden = not self.hidden
        if self.engine:
            self.engine.hidden = self.hidden
        self._on_status(Status("hidden", "Activity hidden") if self.hidden else Status("idle", "Resuming…"))

    def _open_settings(self, *_: object) -> None:
        write_starter_config(self.config_file)
        _open_file(self.config_file)

    def _open_log(self, *_: object) -> None:
        path = log_path()
        if not path.exists():
            path.touch()
        _open_file(path)

    def _toggle_autostart(self, *_: object) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except OSError as e:
            logger.error(f"Couldn't change Start with Windows, {e}")
            self._notify("Couldn't change the Start with Windows setting.")

    def _quit(self, *_: object) -> None:
        self.quitting.set()
        self.reload.set()  # wake the supervisor if it's waiting
        if self.engine:
            self.engine.stop()
        self.icon.stop()

    # ----------------------------------------------------------- engine

    def _watch_config(self) -> None:
        """Restart the engine whenever config.json is saved."""

        last = _mtime(self.config_file)

        while not self.quitting.wait(CONFIG_POLL):
            current = _mtime(self.config_file)

            if current != last:
                last = current
                logger.info("Settings changed, reloading")
                self.reload.set()
                if self.engine:
                    self.engine.stop()

    def _supervise(self) -> None:
        """Run the engine, restarting it after errors and settings changes."""

        if self.first_run:
            write_starter_config(self.config_file)
            self._notify("Welcome! Add your Discord app ID and TMDB API key in Settings. Opening settings…")
            _open_file(self.config_file)

        while not self.quitting.is_set():
            self.reload.clear()

            try:
                config = load_config(self.config_file)
            except Exception as e:
                logger.error(f"Can't read {self.config_file}, {e}")
                self._on_status(Status("error", "config.json has an error — open Settings"))
                self.reload.wait()  # until the file is saved again
                continue

            setup_logging(config["logging"].get("severity", "INFO"), str(log_path()))

            engine = Perplex(config, on_status=self._on_status, open_url=self._open_url, interactive=False)
            engine.hidden = self.hidden
            self.engine = engine

            try:
                engine.run()
            except PermissionError as e:
                logger.error(str(e))
                self._on_status(Status("error", "Plex sign-in wasn't completed — retrying in 1 min"))
                self.reload.wait(RETRY_AFTER_ERROR)
            except OSError as e:  # includes requests/network errors
                logger.error(f"Can't reach Plex, {e}")
                self._on_status(Status("error", "Can't reach Plex — retrying in 1 min"))
                self.reload.wait(RETRY_AFTER_ERROR)
            except Exception as e:
                logger.exception(f"Perplex stopped, {e}")
                self._on_status(Status("error", "Something went wrong — see Open log"))
                self.reload.wait(RETRY_AFTER_ERROR)
            finally:
                self.engine = None

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        threading.Thread(target=self._watch_config, name="config-watch", daemon=True).start()
        self.worker = threading.Thread(target=self._supervise, name="perplex", daemon=True)
        self.worker.start()

    def run(self) -> None:
        self.icon.run(setup=self._setup)

        # Icon closed: let the engine clear the Discord presence before exiting.
        self.quitting.set()
        self.reload.set()
        if self.engine:
            self.engine.stop()
        if worker := getattr(self, "worker", None):
            worker.join(timeout=5)


def main() -> None:
    if not _single_instance():
        _message_box("Perplex is already running. Look for its icon in the system tray.")
        return

    setup_logging("INFO", str(log_path()))
    logger.info("Perplex tray app starting")
    TrayApp().run()
