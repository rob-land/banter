"""Banter — image loading/caching and avatar utilities."""

import hashlib
import threading
import urllib.request

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, GdkPixbuf

from .constants import CACHE_DIR, APP_VERSION, dbg

from pathlib import Path


# ─────────────────────────── Image Helpers ───────────────────────

def _cache_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def load_image_async(url: str, callback, avatar: bool = False):
    """Download and cache image; call callback(path_or_None) on main thread."""
    if not url:
        GLib.idle_add(callback, None)
        return

    def worker():
        key  = _cache_key(url)
        path = CACHE_DIR / f"{key}.img"
        if not path.exists():
            dbg("img-fetch: %s", url)
            try:
                req = urllib.request.Request(url)
                req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                    path.write_bytes(data)
                    dbg("img-fetch: saved %d bytes → %s", len(data), path.name)
            except Exception as e:
                dbg("img-fetch: failed %s – %s", url, e)
                GLib.idle_add(callback, None)
                return
        else:
            dbg("img-cache: hit %s", path.name)
        GLib.idle_add(callback, str(path))

    threading.Thread(target=worker, daemon=True).start()


def set_avatar_from_url(avatar_widget: Adw.Avatar, url: str):
    if not url:
        return

    def on_loaded(path):
        if path:
            try:
                texture = Gdk.Texture.new_from_filename(path)
                avatar_widget.set_custom_image(texture)
            except Exception:
                pass

    load_image_async(url, on_loaded)


# ─────────────────────────── CSS ─────────────────────────────────

APP_CSS = """
/* ── Message bubbles ── */
.msg-bubble {
    border-radius: 18px;
    padding: 8px 14px;
}
.msg-bubble.mine {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
    border-bottom-right-radius: 4px;
}
.msg-bubble.theirs {
    background-color: @card_bg_color;
    border-bottom-left-radius: 4px;
}

/* ── Reaction pills ── */
.reaction-pill {
    border-radius: 999px;
    padding: 0 6px;
    min-height: 24px;
    font-size: 0.85em;
}
.reaction-pill-mine {
    background-color: alpha(@accent_bg_color, 0.18);
    color: @accent_fg_color;
}

/* ── New-messages banner ── */
.new-msg-bar {
    border-radius: 999px;
    padding: 4px 16px;
    font-size: 0.85em;
}

/* ── Date separators ── */
.date-separator {
    margin-top: 10px;
    margin-bottom: 6px;
}
.date-separator-label {
    background-color: alpha(@card_fg_color, 0.08);
    border-radius: 999px;
    padding: 2px 14px;
    font-size: 0.78em;
    font-weight: 600;
    color: @dim_label_color;
}

/* ── Album photo grid ── */
.album-thumb {
    border-radius: 8px;
}
.album-thumb:hover {
    background-color: alpha(@accent_bg_color, 0.12);
}
.album-thumb picture {
    border-radius: 8px;
}

/* ── Input bar ── */
.compose-bar {
    border-top: 1px solid alpha(@borders, 0.5);
    padding: 8px;
}

/* ── Sidebar ── */
.group-list-row {
    padding: 4px 0;
}

/* ── Status badges ── */
.count-badge {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
    border-radius: 999px;
    padding: 1px 7px;
    font-size: 0.78em;
    font-weight: bold;
}

/* ── Unread indicator — blue dot or count pill ── */
.unread-dot {
    background-color: #3584e4;
    border-radius: 999px;
    min-width: 8px;
    min-height: 8px;
    padding: 0;
}
.unread-count {
    background-color: #3584e4;
    color: white;
    border-radius: 999px;
    padding: 1px 6px;
    font-size: 0.72em;
    font-weight: bold;
    min-width: 18px;
}
/* Keep old classes for DM rows that still use them */
.unread-badge {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
    border-radius: 999px;
    padding: 1px 8px;
    font-size: 0.75em;
    font-weight: bold;
    min-width: 18px;
}
.unread-badge-zero {
    color: alpha(@window_fg_color, 0.35);
    border-radius: 999px;
    padding: 1px 8px;
    font-size: 0.75em;
    min-width: 18px;
}

/* ── Muted icon ── */
.muted-icon {
    color: alpha(@window_fg_color, 0.4);
}

/* ── Conversation row time label ── */
.conv-time {
    font-size: 0.75em;
    color: alpha(@window_fg_color, 0.5);
}
.online-dot {
    background-color: #3db93d;
    border-radius: 50%;
    min-width: 10px;
    min-height: 10px;
}

/* ── Error / hint labels ── */
.error-label  { color: @error_color; }
.dim-caption  { font-size: 0.82em; }
.bold-name    { font-weight: 600; }

/* ── Image attachments ── */
.attachment-frame {
    border-radius: 12px;
}

/* ── Login page ── */
.login-card {
    background-color: @card_bg_color;
    border-radius: 16px;
    padding: 24px;
    box-shadow: 0 2px 12px alpha(black, 0.15);
}
"""


# ─────────────────────────── Reusable Widgets ────────────────────
