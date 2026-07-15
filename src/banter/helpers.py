"""Banter — image loading/caching and avatar utilities."""

import hashlib
import logging
import threading
import urllib.request

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

from .async_utils import run_in_background
from .constants import APP_VERSION, CACHE_DIR

log = logging.getLogger(__name__)


# ─────────────────────────── Message helpers ─────────────────────

def format_preview(text, attachments=None) -> str:
    """Build the sidebar/notification preview snippet for a message.

    Substitutes a friendly placeholder for the server-injected
    "⚠️You received a voice note. Please update to the latest version
    of GroupMe..." downgrade-warning text that GroupMe sets on every
    `audio` (voice-note) message — without this, every voice-note
    conversation displays a "please update" warning in the sidebar.

    Detects voice notes via the `audio` attachment type when the
    caller has the attachment list, and falls back to a text-prefix
    match for preview-only payloads (some `messages.preview` shapes
    don't include attachments)."""
    text = (text or "").strip()
    attachments = attachments or []
    has_audio = any(
        isinstance(a, dict) and a.get("type") == "audio"
        for a in attachments)
    if has_audio:
        return "🎤 Voice message"
    # Fallback when only the text was forwarded. The leading "⚠"
    # (U+26A0) is consistent across the localised variants we've seen.
    # We also require "voice note" to keep the rule tight in case
    # GroupMe re-uses the warning prefix for an unrelated downgrade.
    if text.startswith("⚠") and "voice note" in text.lower():
        return "🎤 Voice message"
    if text:
        return text
    if attachments:
        return "📎 attachment"
    return ""


def resolve_group_last_message(api, group: dict) -> dict:
    """Fetch a group's real newest message, for callers that detected
    a last_message_id change in a /groups index response.

    The index's `messages.preview` blob skips system messages
    entirely: when the newest message is one ("X left the group",
    a name change, …) the preview still shows the last human message,
    so building a notification from it surfaces stale text. Falls
    back to a preview-shaped pseudo-message if the fetch fails so
    callers degrade to the old preview-based behaviour.

    Blocking — call from a worker thread."""
    gid = str(group["id"])
    try:
        msgs = api.get_messages(gid, limit=1)
        if msgs:
            return msgs[0]
    except Exception as e:
        log.debug("last-message fetch failed for group %s: %s", gid, e)
    preview = (group.get("messages") or {}).get("preview") or {}
    return {
        "name":        preview.get("nickname") or "",
        "text":        preview.get("text"),
        "attachments": preview.get("attachments") or [],
    }


def is_hidden_system_message(msg: dict) -> bool:
    """Return True if `msg` is a GroupMe-issued system notification
    that the UI should suppress entirely.

    GroupMe injects a synthetic message (system=True, sender "GroupMe")
    after every edit and delete — e.g. 'Rob Daniel edited to: "..."'
    or 'A message was deleted.'. Both are noise for our UI: we already
    update the original bubble in place on edits and remove it on
    deletes, so the system message just duplicates the information.

    Other system messages (joins, name changes, event creation, etc.)
    are meaningful and stay visible.
    """
    if not msg.get("system"):
        return False
    text = (msg.get("text") or "").lower()
    # Edit notifications — GroupMe's exact phrasing is
    # `<Sender> edited to: "<new text>"`.
    if "edited to:" in text:
        return True
    # Delete notifications — exact text varies slightly
    # ("A message was deleted." / "<Sender> deleted a message.")
    if "was deleted" in text or "deleted a message" in text:
        return True
    return False


# ─────────────────────────── Image Helpers ───────────────────────

def _cache_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def load_image_async(url: str, callback, avatar: bool = False):
    """Download and cache image; call callback(path_or_None) on main thread.

    Failed fetches (4xx / 5xx) are remembered via a sibling `.fail`
    marker so the same dead URL doesn't get re-requested on every
    re-render — old GroupMe images return 403 forever, and a chat
    with even a few of them used to fire a fresh network round-trip
    per bubble per scroll/refresh."""
    if not url:
        GLib.idle_add(callback, None)
        return

    def worker():
        key  = _cache_key(url)
        path = CACHE_DIR / f"{key}.img"
        fail = CACHE_DIR / f"{key}.fail"
        if path.exists():
            log.debug("img-cache: hit %s", path.name)
            GLib.idle_add(callback, str(path))
            return
        if fail.exists():
            # Previously known-bad URL — don't hit the network again.
            GLib.idle_add(callback, None)
            return
        log.debug("img-fetch: %s", url)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
                path.write_bytes(data)
                log.debug("img-fetch: saved %d bytes → %s", len(data), path.name)
        except Exception as e:
            log.debug("img-fetch: failed %s – %s", url, e)
            try:
                fail.write_bytes(b"")
            except Exception:
                pass
            GLib.idle_add(callback, None)
            return
        GLib.idle_add(callback, str(path))

    run_in_background(worker)


def load_texture_async(url: str, max_w: int, max_h: int, callback):
    """Fetch (or cache-hit) `url`, decode + scale to fit
    `max_w`×`max_h`, and call `callback(Gdk.Texture | None)` on the
    main thread.

    Why: when many cached images land at once (opening a chat with
    yesterday's photo dump), per-image decode+scale on the main loop
    blocks long enough for the compositor to put up a "Banter is
    failing to respond" prompt. This helper keeps the decode on the
    worker thread so only the texture handoff hits the main loop.

    The size caps prevent a 4 MP photo from becoming a multi-megabyte
    texture for a 280-px bubble. Failure modes match `load_image_async`:
    network error → `.fail` marker, future cache miss → callback(None)."""
    if not url:
        GLib.idle_add(callback, None)
        return

    def worker():
        key  = _cache_key(url)
        path = CACHE_DIR / f"{key}.img"
        fail = CACHE_DIR / f"{key}.fail"
        if not path.exists():
            if fail.exists():
                GLib.idle_add(callback, None)
                return
            log.debug("img-fetch: %s", url)
            try:
                req = urllib.request.Request(url)
                req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
                with urllib.request.urlopen(req, timeout=15) as r:
                    path.write_bytes(r.read())
            except Exception as e:
                log.debug("img-fetch: failed %s – %s", url, e)
                try:
                    fail.write_bytes(b"")
                except Exception:
                    pass
                GLib.idle_add(callback, None)
                return
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path), max_w, max_h, True)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        except Exception as e:
            log.debug("load_texture: decode failed for %s: %s", path, e)
            GLib.idle_add(callback, None)
            return
        GLib.idle_add(callback, texture)

    run_in_background(worker)


def load_video_async(url: str, callback):
    """Download a video attachment to CACHE_DIR (streaming in chunks
    so multi-megabyte clips don't sit in memory) and call
    `callback(path_or_None)` on the main thread.

    Why route playback through a local file: `Gtk.Video.set_file` on a
    remote URI hands the work to `playbin` + `souphttpsrc`. If the HTTP
    fetch (TLS handshake, redirect, range request) hiccups, the
    pipeline ends up with no source pad and gtkgst raises
    `gtk_gst_media_file_source_setup_cb: assertion 'stream != NULL'`,
    which surfaces as a video that never starts. Caching to disk first
    sidesteps that failure mode and gives "Save Video…" a free
    pre-warmed source."""
    if not url:
        GLib.idle_add(callback, None)
        return

    def worker():
        key  = _cache_key(url)
        path = CACHE_DIR / f"{key}.video"
        fail = CACHE_DIR / f"{key}.video.fail"
        if path.exists():
            log.debug("video-cache: hit %s", path.name)
            GLib.idle_add(callback, str(path))
            return
        if fail.exists():
            GLib.idle_add(callback, None)
            return
        log.debug("video-fetch: %s", url)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
            with urllib.request.urlopen(req, timeout=30) as r, \
                 open(path, "wb") as out:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        except Exception as e:
            log.debug("video-fetch: failed %s – %s", url, e)
            try:
                if path.exists():
                    path.unlink()
                fail.write_bytes(b"")
            except Exception:
                pass
            GLib.idle_add(callback, None)
            return
        GLib.idle_add(callback, str(path))

    run_in_background(worker)


def load_audio_async(api, url: str, callback):
    """Download and cache a voice-note audio file; call
    callback(path_or_None) on the main thread.

    Mirrors `load_image_async`'s caching pattern (key.audio + key.audio.fail),
    but routes through `api.download_audio` because m.groupme.com requires
    a `Cookie: token=...` header that the unauth'd image fetcher lacks."""
    if not url:
        GLib.idle_add(callback, None)
        return

    def worker():
        key  = _cache_key(url)
        path = CACHE_DIR / f"{key}.audio"
        fail = CACHE_DIR / f"{key}.audio.fail"
        if path.exists():
            log.debug("audio-cache: hit %s", path.name)
            GLib.idle_add(callback, str(path))
            return
        if fail.exists():
            GLib.idle_add(callback, None)
            return
        log.debug("audio-fetch: %s", url)
        ok = api.download_audio(url, str(path))
        if not ok:
            try:
                if path.exists():
                    path.unlink()
                fail.write_bytes(b"")
            except Exception:
                pass
            GLib.idle_add(callback, None)
            return
        GLib.idle_add(callback, str(path))

    run_in_background(worker)


def set_avatar_from_url(avatar_widget: Adw.Avatar, url: str):
    if not url:
        log.debug("set_avatar: empty url, skipping")
        return

    def on_loaded(path):
        if not path:
            log.debug("set_avatar: no path for %s (cache miss + fetch failed)", url)
            return
        try:
            texture = Gdk.Texture.new_from_filename(path)
        except Exception as e:
            # Log instead of silent swallow — old GroupMe URLs sometimes
            # serve content that GdkPixbuf doesn't recognise.
            log.debug("set_avatar: texture load failed for %s: %s", path, e)
            return
        try:
            avatar_widget.set_custom_image(texture)
        except Exception as e:
            log.debug("set_avatar: set_custom_image failed for %s: %s", path, e)

    load_image_async(url, on_loaded)


# ─────────────────────────── GroupMe Powerups (emoji packs) ──────────
#
# GroupMe's proprietary emoji packs ("Powerups") are distributed as
# spritesheets. A reaction / attachment references a pack by `pack_id`
# plus an `offset` into the sheet. We:
#   1. Load the catalog once (ensure_packs_loaded)
#   2. Download spritesheets on demand, cache per-emoji crops to disk
#   3. Expose set_pack_emoji(widget, pack_id, offset, size) to render one
#
# The catalog is kept in module-level state (single-account assumption —
# packs are account-independent since they come from the public catalog).

_PACK_REGISTRY: dict = {}          # int pack_id → pack dict
_PACK_REGISTRY_LOADED = False
_PACK_LOAD_IN_FLIGHT = False       # True while a worker is fetching
_PACK_LOAD_WAITERS = []            # callbacks to fire once loaded
_PACK_LOAD_LOCK = threading.Lock() # guards the three fields above

# Which DPI we prefer when picking a variant from meta.inline / meta.icon.
# 320 (xhdpi = 40×40 inline cells) is a reasonable balance between crispness
# and download size. If not present, fall back to the first entry.
_PREFERRED_DENSITY = 320


def ensure_packs_loaded(api, callback=None):
    """Fetch the powerups catalog once and cache it. `callback(registry)`
    fires on the main thread after loading; fires immediately if already
    loaded. Safe to call multiple times — concurrent calls coalesce."""
    global _PACK_LOAD_IN_FLIGHT
    with _PACK_LOAD_LOCK:
        if _PACK_REGISTRY_LOADED:
            if callback:
                # Deliver immediately — still on caller's thread, like
                # the pre-lock behavior.
                callback(_PACK_REGISTRY)
            return
        if callback:
            _PACK_LOAD_WAITERS.append(callback)
        # Exactly one caller (the first to arrive after a reset) spawns
        # the worker; subsequent calls just queue their callback.
        if _PACK_LOAD_IN_FLIGHT:
            return
        _PACK_LOAD_IN_FLIGHT = True

    def worker():
        global _PACK_REGISTRY_LOADED, _PACK_LOAD_IN_FLIGHT
        try:
            packs = api.get_powerups() or []
            for p in packs:
                if not isinstance(p, dict):
                    continue
                meta = p.get("meta")
                if not isinstance(meta, dict):
                    continue
                pid = meta.get("pack_id")
                if pid is None:
                    continue
                try:
                    _PACK_REGISTRY[int(pid)] = p
                except (TypeError, ValueError):
                    continue
            log.debug("powerups: loaded %d packs", len(_PACK_REGISTRY))
            # Write a summary showing which packs pass the pack_info()
            # validation used by the picker — lets us see WHY specific
            # packs aren't rendering.
            try:
                summary_path = CACHE_DIR / "powerups_summary.txt"
                rows = []
                for pid, p in sorted(_PACK_REGISTRY.items()):
                    name = p.get("name") or "?"
                    info = pack_info(p)
                    if info is None:
                        # Diagnose why
                        meta = p.get("meta") if isinstance(p.get("meta"), dict) else {}
                        reasons = []
                        if not isinstance(meta, dict):
                            reasons.append("no meta")
                        if meta.get("pack_id") is None:
                            reasons.append("no pack_id")
                        t = meta.get("transliterations")
                        if not isinstance(t, list) or not t:
                            reasons.append(f"transliterations={type(t).__name__}"
                                           f"/{len(t) if isinstance(t, list) else '?'}")
                        inl = meta.get("inline")
                        if not isinstance(inl, list) or not inl:
                            reasons.append(f"inline={type(inl).__name__}")
                        else:
                            has_any = any(isinstance(v, dict) and v.get("image_url") for v in inl)
                            if not has_any:
                                reasons.append("inline-no-image_url")
                        status = "DROPPED: " + ", ".join(reasons) if reasons else "DROPPED: other"
                    else:
                        status = f"ok  size={info['pack_size']}  cell={info['cell_w']}x{info['cell_h']}"
                    rows.append(f"{pid:>4}  {name:<30s}  {status}")
                summary_path.write_text(
                    f"Loaded packs ({len(rows)}):\n" + "\n".join(rows))
                log.debug("powerups: summary → %s", summary_path)
            except Exception as e:
                log.debug("powerups: summary failed – %s", e)
        except Exception as e:
            log.debug("powerups: load failed – %s", e)

        # Drain the waiter list atomically with the flag flip so a
        # caller that arrives *between* these two can't miss the
        # notification.
        with _PACK_LOAD_LOCK:
            waiters = list(_PACK_LOAD_WAITERS)
            _PACK_LOAD_WAITERS.clear()
            _PACK_REGISTRY_LOADED = True
            _PACK_LOAD_IN_FLIGHT  = False

        def _flush():
            for cb in waiters:
                try:
                    cb(_PACK_REGISTRY)
                except Exception:
                    pass
            return False
        GLib.idle_add(_flush)

    run_in_background(worker)


def get_pack(pack_id) -> dict | None:
    try:
        return _PACK_REGISTRY.get(int(pack_id))
    except (TypeError, ValueError):
        return None


def get_all_packs() -> list:
    return list(_PACK_REGISTRY.values())


def _pick_variant(variants, density=_PREFERRED_DENSITY):
    """From a list of per-DPI variant dicts, pick the one matching the
    preferred density, or fall back to the first valid dict."""
    if not isinstance(variants, list):
        return None
    for v in variants:
        if isinstance(v, dict) and v.get("density") == density:
            return v
    for v in variants:
        if isinstance(v, dict):
            return v
    return None


def pack_info(pack: dict) -> dict | None:
    """Normalize a raw powerup pack dict into the fields we need.

    Returns None when any required field is missing.
    Shape: {pack_id, name, pack_size, sprite_url, cell_w, cell_h, icon_url}
    """
    if not isinstance(pack, dict):
        return None
    meta = pack.get("meta")
    if not isinstance(meta, dict):
        return None

    pid_raw = meta.get("pack_id")
    if pid_raw is None:
        return None
    try:
        pack_id = int(pid_raw)
    except (TypeError, ValueError):
        return None

    translit = meta.get("transliterations")
    if not isinstance(translit, list) or not translit:
        return None

    inline = _pick_variant(meta.get("inline"))
    if not inline:
        return None
    sprite_url = inline.get("image_url")
    cell_w = int(inline.get("x", 0) or 0)
    cell_h = int(inline.get("y", 0) or 0)
    if not sprite_url or cell_w <= 0 or cell_h <= 0:
        return None

    icon_variant = _pick_variant(meta.get("icon"))
    icon_url = icon_variant.get("image_url") if isinstance(icon_variant, dict) else None

    return {
        "pack_id":    pack_id,
        "name":       pack.get("name", "Pack"),
        "pack_size":  len(translit),
        "sprite_url": sprite_url,
        "cell_w":     cell_w,
        "cell_h":     cell_h,
        "icon_url":   icon_url,
    }


def set_pack_emoji(image_widget: Gtk.Image, pack_id, offset,
                   display_size: int = 24):
    """Asynchronously load a single pack emoji into `image_widget`.

    Steps: download the pack spritesheet to CACHE_DIR, crop the cell at
    `offset` (sheet may be a horizontal strip or a 2D grid — we
    auto-detect from the pixbuf's actual width), scale to
    `display_size`, persist the cropped PNG so subsequent renders skip
    download + crop entirely.

    `pack_id` and `offset` may arrive as strings (GroupMe serializes
    pack_id / pack_index as strings on the wire) — coerce to int."""
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        return False
    pack = get_pack(pack_id)
    if not pack:
        return False
    info = pack_info(pack)
    if not info:
        return False
    url = info["sprite_url"]
    cw  = info["cell_w"]
    ch  = info["cell_h"]

    pid = info["pack_id"]
    cell_cache = CACHE_DIR / f"powerup_{pid}_{offset}_{display_size}.png"
    if cell_cache.exists():
        image_widget.set_from_file(str(cell_cache))
        return True

    sheet_cache = CACHE_DIR / f"powerup_sheet_{pid}.png"

    def worker():
        if not sheet_cache.exists():
            try:
                req = urllib.request.Request(url)
                req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
                with urllib.request.urlopen(req, timeout=15) as r:
                    sheet_cache.write_bytes(r.read())
            except Exception as e:
                log.debug("powerup: sheet download failed (%s): %s", url, e)
                return
        try:
            sheet = GdkPixbuf.Pixbuf.new_from_file(str(sheet_cache))
            sw = sheet.get_width()
            cols = max(1, sw // cw)
            row = offset // cols
            col = offset %  cols
            if (col + 1) * cw > sw or (row + 1) * ch > sheet.get_height():
                log.debug("powerup: offset %d out of bounds in pack %s", offset, pid)
                return
            crop = sheet.new_subpixbuf(col * cw, row * ch, cw, ch)
            if display_size and display_size != cw:
                crop = crop.scale_simple(
                    display_size, display_size, GdkPixbuf.InterpType.BILINEAR)
            crop.savev(str(cell_cache), "png", [], [])
        except Exception as e:
            log.debug("powerup: crop failed pack=%s off=%d – %s", pid, offset, e)
            return
        GLib.idle_add(lambda: image_widget.set_from_file(str(cell_cache)) or False)

    run_in_background(worker)
    return True

