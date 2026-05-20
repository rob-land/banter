"""
Banter — constants and shared utilities.
"""


import sys
from pathlib import Path

# ── Mode flags ──────────────────────────────────────────────────────
# Parsed from argv so other modules can react; flags are stripped from
# sys.argv here so GApplication's own option parser doesn't reject them.
_raw_args = sys.argv[1:]
DEBUG      = any(a in ("--debug", "--verbose", "-v", "-d") for a in _raw_args)
DEMO       = any(a in ("--demo",) for a in _raw_args)
# Headless notification daemon mode — set by the autostart entry point
# and intended to be launched at login.
BACKGROUND = any(a in ("--background",) for a in _raw_args)
sys.argv = [sys.argv[0]] + [
    a for a in _raw_args
    if a not in ("--debug", "--verbose", "-v", "-d", "--demo", "--background")
]

# ── App identity ─────────────────────────────────────────────────────
# Re-exported from the Meson-generated const.py so the rest of the
# package only ever talks to constants. (Ruff's F401 will want to
# remove these as unused — keep them.)
from .const import APP_ID, APP_NAME  # noqa: F401
from .const import VERSION as APP_VERSION  # noqa: F401

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
GROUPME_API      = "https://api.groupme.com/v3"
GROUPME_IMAGE    = "https://image.groupme.com"
GROUPME_PUSH     = "https://push.groupme.com/faye"
GROUPME_POWERUPS = "https://powerup.groupme.com"
GROUPME_FILE     = "https://file.groupme.com"

# ── OAuth ────────────────────────────────────────────────────────────
OAUTH_PORT          = 7654
OAUTH_AUTHORIZE_URL = "https://oauth.groupme.com/oauth/authorize"

# ── Default reactions ────────────────────────────────────────────────
# Broad set of common reactions. We initially thought GroupMe gated the
# set, but the 500s we saw were actually the wrong endpoint/body —
# /messages/{gid}/{mid}/like with a like_icon body accepts any unicode
# or ZWJ-sequence emoji.
DEFAULT_REACTIONS = [
    # Hearts + affection
    "❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎",
    "❤️‍🔥","💖","💗","💓","💞","💕","💘","💝","💔",
    # Smiley faces
    "😂","🤣","😊","😇","🙂","😎","🥰","😍","😘","🥲",
    "🤔","🙄","😐","😏","😑","😴","🥱","🤤","🤗","🤭",
    "🤫","🤐","🤪","🥳","🤓","🧐","😮","😯","😲","🤯",
    "😢","😭","🥺","😤","😠","😡","🤬","🤮","🤢","🤕",
    "😱","😨","😰","😓","🥶","🥵","🫠","🫡","🫢",
    # Gestures + hands
    "👍","👎","👏","🙏","🫶","🤝","🤲","👐","🙌","💪",
    "✌️","🤞","🤟","🤘","🤙","👌","🫰","👀","🤷","🤦",
    # Objects + symbols
    "🔥","🎉","✨","💯","💀","☠️","💩","🐐","👑","💎",
    "🎂","🍕","☕","🍺","🍻","🥂","🌟","⭐","⚡","☀️",
    "✅","❌","⚠️","💅","🗣️","💬","👋","🫣","🫵","🙈",
]

# ── Markup helper ────────────────────────────────────────────────────
def esc(s: str) -> str:
    """Escape a plain string for safe use in Pango markup / GTK titles."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
