"""Banter — AlbumCard: inline album preview inside a message bubble."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, GdkPixbuf, Gdk, Pango

from ..constants import esc
from ..async_utils import run_in_background
from ..helpers import load_image_async


class AlbumCard(Gtk.Box):
    """Inline album card rendered inside a MessageBubble for a
    `gallery.album.create` (or `.add.media`) event message.

    Shows a cover thumbnail, the album title, and a photo / video
    count. Clicking the card or the explicit "Open Album" button
    opens `AlbumViewDialog` for full browsing.

    The album metadata embedded on the message (event.data.album)
    only carries the cover slot at create time — `media_count` is 1
    with an empty `media_urls[0]`. To get accurate `total_images` /
    `total_videos` and a real cover URL we lazy-fetch the album via
    `api.get_album(gid, album_id)`.
    """

    THUMB_W, THUMB_H = 96, 72

    def __init__(self, api, gid, album_data: dict, window):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add_css_class("event-card")   # reuse the existing card style

        self.api = api
        self.gid = str(gid)
        self.win = window
        self._album_id    = str(album_data.get("album_id") or "")
        self._album_title = album_data.get("album_title") or "Album"
        self._album_full  = None   # populated by _on_fetched

        # ── Cover thumbnail (left) ─────────────────────────────
        self._thumb = Gtk.Picture()
        self._thumb.set_can_shrink(True)
        self._thumb.set_content_fit(Gtk.ContentFit.COVER)
        self._thumb.set_size_request(self.THUMB_W, self.THUMB_H)
        self._thumb.add_css_class("attachment-frame")
        self.append(self._thumb)

        # ── Right column: title + counts + Open button ────────
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_hexpand(True)
        right.set_valign(Gtk.Align.CENTER)

        hdr = Gtk.Box(spacing=6)
        icon = Gtk.Image.new_from_icon_name("folder-pictures-symbolic")
        icon.set_pixel_size(16)
        hdr.append(icon)
        self._title_lbl = Gtk.Label(label=esc(self._album_title))
        self._title_lbl.add_css_class("heading")
        self._title_lbl.set_xalign(0)
        self._title_lbl.set_hexpand(True)
        self._title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_lbl.set_max_width_chars(28)
        hdr.append(self._title_lbl)
        right.append(hdr)

        self._count_lbl = Gtk.Label(label="Loading album…")
        self._count_lbl.add_css_class("dim-caption")
        self._count_lbl.set_xalign(0)
        right.append(self._count_lbl)

        # Open button
        open_btn = Gtk.Button(label="Open Album")
        open_btn.add_css_class("pill")
        open_btn.set_halign(Gtk.Align.START)
        open_btn.set_margin_top(2)
        open_btn.connect("clicked", self._on_open_clicked)
        right.append(open_btn)

        self.append(right)

        # Make the whole header clickable too. RSVP-style — the
        # button itself stays the canonical action, but a tap on the
        # title or thumbnail opens the dialog as well.
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        for target in (self._thumb, hdr):
            gest = Gtk.GestureClick()
            gest.connect("pressed", self._on_open_clicked)
            target.add_controller(gest)

        if self._album_id:
            self._fetch()
        else:
            self._count_lbl.set_text("Album unavailable")

    # ── Fetch full album metadata ──────────────────────────────
    def _fetch(self):
        gid = self.gid
        aid = self._album_id
        api = self.api

        def worker():
            album = api.get_album(gid, aid)
            GLib.idle_add(self._on_fetched, album)

        run_in_background(worker)

    def _on_fetched(self, album):
        if not album:
            self._count_lbl.set_text("Album unavailable")
            return
        self._album_full = album
        # Title might be richer in the fetched dict (renames are
        # possible, though rare). Prefer the fresh value.
        title = album.get("title") or self._album_title
        self._title_lbl.set_label(esc(title))
        self._album_title = title

        imgs   = int(album.get("total_images") or 0)
        videos = int(album.get("total_videos") or 0)
        if imgs or videos:
            parts = []
            if imgs:
                parts.append(f"{imgs} photo" + ("s" if imgs != 1 else ""))
            if videos:
                parts.append(f"{videos} video" + ("s" if videos != 1 else ""))
            self._count_lbl.set_text(" · ".join(parts))
        else:
            self._count_lbl.set_text("Empty album")

        cover = album.get("cover_image_url") or ""
        if cover:
            load_image_async(cover, self._on_cover_loaded)

    def _on_cover_loaded(self, path):
        if not path:
            return
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, self.THUMB_W * 2, self.THUMB_H * 2, False)
            ok, buf = pix.save_to_bufferv("png", [], [])
            if ok:
                self._thumb.set_paintable(
                    Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf)))
        except Exception:
            pass

    # ── Open Album → AlbumViewDialog ───────────────────────────
    def _on_open_clicked(self, *_):
        # Late import to avoid the circular dialogs ↔ widgets import at
        # module-load time (gallery.py pulls in the gallery dialogs
        # which in turn pull in widgets).
        from ..dialogs.gallery import AlbumViewDialog
        album = self._album_full or {
            "album_id": self._album_id,
            "title":    self._album_title,
        }
        AlbumViewDialog(
            self.api, {"id": self.gid}, album, self.win).present(self.win)
