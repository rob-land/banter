"""Logging configuration.

Banter runs as a Flatpak on Phosh in production, where the app's stdout
is captured by `journalctl --user` but not always reachable from the
phone itself. Each run mirrors its log to a rotating file the user can
pull off the phone:

    ~/.local/share/banter/banter.log          (host install)
    ~/.var/app/land.rob.banter/data/banter/banter.log
                                              (Flatpak)

Default level is INFO. Set `BANTER_DEBUG=1` in the environment, or
pass `--debug` / `-d` / `--verbose` / `-v` on the command line, to
bump to DEBUG.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from gi.repository import GLib

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_LOG_FILE_BYTES = 512 * 1024
_LOG_BACKUPS    = 2

_DEBUG_FLAGS = ("--debug", "-d", "--verbose", "-v")


def log_dir() -> str:
    path = os.path.join(GLib.get_user_data_dir(), "banter")
    os.makedirs(path, exist_ok=True)
    return path


def log_path() -> str:
    return os.path.join(log_dir(), "banter.log")


def is_debug() -> bool:
    if any(flag in sys.argv for flag in _DEBUG_FLAGS):
        return True
    val = os.environ.get("BANTER_DEBUG", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def configure_logging() -> None:
    """Configure the root logger. Idempotent — repeated calls reset
    handlers cleanly so a re-init from main() doesn't double-log.
    """
    level = logging.DEBUG if is_debug() else logging.INFO
    fmt = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        path = log_path()
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_LOG_FILE_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        logging.getLogger(__name__).info(
            "logging configured: level=%s file=%s",
            logging.getLevelName(level), path)
    except Exception:
        logging.getLogger(__name__).exception(
            "file logging setup failed; continuing with stream only")
