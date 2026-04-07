"""
Banter — constants, logging, and shared utilities.
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')

from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf

import sys
import logging
import time
from pathlib import Path

# ── Debug setup ─────────────────────────────────────────────────────
_raw_args = sys.argv[1:]
DEBUG = any(a in ("--debug", "--verbose", "-v", "-d") for a in _raw_args)
sys.argv = [sys.argv[0]] + [
    a for a in _raw_args
    if a not in ("--debug", "--verbose", "-v", "-d")
]

logging.basicConfig(
    level   = logging.DEBUG if DEBUG else logging.WARNING,
    format  = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt = "%H:%M:%S",
    stream  = sys.stderr,
)
log = logging.getLogger("banter")

def dbg(msg: str, *args):
    if DEBUG:
        log.debug(msg, *args)

# ── App identity ─────────────────────────────────────────────────────
APP_ID      = "land.rob.Banter"
APP_NAME    = "Banter"
APP_VERSION = "1.0.0"

# ── Paths ────────────────────────────────────────────────────────────
# Respect XDG Base Directory spec — Flatpak sets these env vars to point
# inside its sandbox, so hardcoding ~/.config would resolve to the
# read-only host path instead of the writable sandbox location.
import os as _os
_xdg_config = Path(_os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
_xdg_cache  = Path(_os.environ.get("XDG_CACHE_HOME",  Path.home() / ".cache"))
CONFIG_DIR  = _xdg_config / "banter"
CACHE_DIR   = _xdg_cache  / "banter"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
EMOJI_LOG   = CONFIG_DIR / "emoji_reactions.log"

# ── GroupMe endpoints ────────────────────────────────────────────────
GROUPME_API   = "https://api.groupme.com/v3"
GROUPME_IMAGE = "https://image.groupme.com"
GROUPME_PUSH  = "https://push.groupme.com/faye"

# ── OAuth ────────────────────────────────────────────────────────────
OAUTH_PORT          = 7654
OAUTH_AUTHORIZE_URL = "https://oauth.groupme.com/oauth/authorize"

# ── Default reactions ────────────────────────────────────────────────
DEFAULT_REACTIONS = [
    "❤️","👍","👎","😂","🤣","😮","😢","😭","🔥","🎉",
    "👀","🙏","💀","🫶","🤬","💅","🫠","🥺","😍","🤔",
]

# ── Markup helper ────────────────────────────────────────────────────
def esc(s: str) -> str:
    """Escape a plain string for safe use in Pango markup / GTK titles."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
