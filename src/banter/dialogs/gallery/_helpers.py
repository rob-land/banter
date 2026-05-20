"""Shared gallery / album helpers — toasts, uploads, bulk download.

Imported by the dialog modules in this package; not part of the
public surface.
"""

import logging
import re
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from gi.repository import Gio, GLib, Gtk

from ...async_utils import run_in_background
from ...constants import APP_VERSION, CACHE_DIR
from ...helpers import _cache_key

log = logging.getLogger(__name__)


def _toast(parent, text):
    try:
        parent.toast(text)
    except Exception:
        pass


def _add_media(api, group, album, media, parent, on_done):
    """Shared "POST media to this album" worker used by both the
    existing-album and new-album flows."""
    gid = group["id"]
    aid = album["album_id"]
    title = album.get("title", "album")
    n = len(media)

    def worker():
        r = api.add_to_album(gid, aid, media)
        ok = r is not None

        def report():
            try:
                if ok:
                    parent.toast(
                        f"Added {n} item{'s' if n != 1 else ''} to '{title}'")
                else:
                    parent.toast("Failed to add to album")
            except Exception:
                pass
            if callable(on_done):
                on_done(ok)
        GLib.idle_add(report)

    run_in_background(worker)


def add_local_image_to_album(api, gid: str, album: dict,
                              parent, on_done=None):
    """Pick a local image, upload it, then attach it to `album`.

    Album entries require a public `media_url` (i.groupme.com / cdn
    form). `api.upload_image` returns one; the matching album POST
    is `api.add_to_album` with a `[{media_url, media_type, "image",
    media_source: "album"}]` body.

    Videos are intentionally not offered yet — `file.groupme.com`
    uploads return a `file_id`, which doesn't fit the album shape,
    and we haven't captured a video-upload-with-media-url path.

    `on_done()` fires after a successful add so the caller (album
    viewer / album card) can refresh its tile flow / counts.
    """
    fd = Gtk.FileDialog()
    fd.set_title("Add Photo to Album")
    flt = Gtk.FileFilter()
    flt.set_name("Images")
    for mime in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        flt.add_mime_type(mime)
    store = Gio.ListStore.new(Gtk.FileFilter)
    store.append(flt)
    fd.set_filters(store)
    fd.set_default_filter(flt)

    aid = album.get("album_id") or album.get("id") or ""
    title = album.get("title") or "album"

    def on_picked(_fd, result):
        try:
            f = fd.open_finish(result)
            path = f.get_path()
        except GLib.Error:
            return   # user cancelled
        try:
            parent.toast("Uploading photo…")
        except Exception:
            pass

        def worker():
            url = api.upload_image(path)
            if not url:
                GLib.idle_add(lambda: _toast(parent, "Upload failed"))
                return
            media = [{"media_url":    url,
                      "media_type":   "image",
                      "media_source": "album"}]
            r = api.add_to_album(gid, aid, media)
            ok = r is not None

            def report():
                _toast(parent, f"Added to '{title}'" if ok else "Add failed")
                if ok and callable(on_done):
                    try:
                        on_done()
                    except Exception:
                        pass
            GLib.idle_add(report)

        run_in_background(worker)

    fd.open(parent, None, on_picked)


# ─────────────────────────── Bulk download ────────────────────────

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")


def _safe_fs_name(name: str) -> str:
    """Strip / replace characters that are awkward in filesystem paths.
    Falls back to a generic label if everything's stripped."""
    cleaned = re.sub(r'[\/\\:\*\?"<>\|\x00-\x1f]', '_', name).strip(" .")
    return cleaned[:80] or "album"


def _ext_for(url: str, kind: str) -> str:
    """Infer a sensible file extension. GroupMe URLs often end with a
    size suffix like '.jpg.large' or '.png.preview'; scan the path
    for the first known media extension instead of taking the tail."""
    path = urlsplit(url).path.lower()
    for ext in _IMAGE_EXTS + _VIDEO_EXTS:
        if ext in path:
            return ext
    return ".mp4" if kind == "video" else ".jpg"


def _bulk_download(items: list, dest_root: str, parent) -> None:
    """Spawn a worker that fetches each item in ``items`` and writes
    it under ``dest_root``. Toasts on ``parent`` when starting and
    when complete. Safe to call from the GTK main thread.

    Each item is a dict: ``{"url": str, "kind": "image"|"video",
    "subdir": str}``. ``subdir`` may contain slashes for nested
    directories under ``dest_root``."""
    if not items:
        try:
            parent.toast("Nothing to download")
        except Exception:
            pass
        return

    try:
        parent.toast(
            f"Downloading {len(items)} item{'s' if len(items) != 1 else ''}…")
    except Exception:
        pass

    def worker():
        _do_bulk_download(items, dest_root, parent)

    run_in_background(worker)


def _do_bulk_download(items: list, dest_root: str, parent) -> None:
    """Run on a worker thread: fetch + save each item, then toast.

    Image URLs are served from cache when available (the gallery has
    typically already populated the cache during thumbnail render);
    misses and videos are fetched fresh."""
    dest = Path(dest_root)
    # Counter per subdir for non-colliding filenames.
    seq: dict = {}
    saved = 0

    for item in items:
        url = item.get("url", "")
        kind = item.get("kind", "image")
        subdir = item.get("subdir", "") or ""
        if not url:
            continue

        target_dir = dest / subdir if subdir else dest
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.debug("bulk-dl: mkdir %s failed: %s", target_dir, exc)
            continue

        idx = seq.get(subdir, 0) + 1
        seq[subdir] = idx
        out = target_dir / f"{idx:04d}{_ext_for(url, kind)}"

        try:
            # Hit the cache for images that the thumbnail render
            # already fetched. Videos are not cached as raw files,
            # so they go straight to network.
            cached = CACHE_DIR / f"{_cache_key(url)}.img"
            if kind == "image" and cached.exists():
                shutil.copy(cached, out)
            else:
                req = urllib.request.Request(url)
                req.add_header("User-Agent",
                               f"GroupMe-GNOME/{APP_VERSION}")
                with urllib.request.urlopen(req, timeout=60) as r:
                    out.write_bytes(r.read())
            saved += 1
        except Exception as exc:
            log.debug("bulk-dl: failed %s: %s", url, exc)

    total = len(items)

    def report():
        try:
            if saved == total:
                parent.toast(f"Downloaded {saved} item{'s' if saved != 1 else ''}")
            elif saved == 0:
                parent.toast("Download failed")
            else:
                parent.toast(
                    f"Downloaded {saved} of {total} items (some failed)")
        except Exception:
            pass

    GLib.idle_add(report)
