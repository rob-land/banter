"""Banter — GalleryDialog (group image gallery)."""

import shutil
from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf, Pango

from ..constants import esc, CACHE_DIR
from ..async_utils import run_in_background
from ..helpers import load_image_async, _cache_key
from ..widgets.base import StandardDialog
from ..widgets.misc import VideoAttachment


class GalleryDialog(StandardDialog):
    """Browse all images sent in a group (the real GroupMe gallery API).

    GroupMe has no concept of separate albums via its public API.
    The gallery endpoint returns all messages that contain images,
    paginated by timestamp newest-first.
    """

    PAGE_SIZE = 100
    THUMB_SIZE = 120

    def __init__(self, api, group, parent):
        super().__init__(title=f"Gallery – {group.get('name','')}",
                         width=520, height=660)
        self._api        = api
        self._group      = group
        self._parent     = parent
        self._messages   = []          # accumulated message dicts
        self._oldest_ts  = None        # gallery_ts of oldest fetched msg
        self._loading    = False
        self._exhausted  = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Upload button in header (rightmost)
        up_btn = Gtk.Button(icon_name="document-send-symbolic")
        up_btn.add_css_class("flat")
        up_btn.set_tooltip_text("Upload photo to group")
        up_btn.connect("clicked", self._pick_photo)
        self.add_header_widget(up_btn, end=True)

        # New album button (left of upload)
        new_album_btn = Gtk.Button(icon_name="folder-new-symbolic")
        new_album_btn.add_css_class("flat")
        new_album_btn.set_tooltip_text("Create new album")
        new_album_btn.connect("clicked", self._on_new_album)
        self.add_header_widget(new_album_btn, end=True)

        # Spinner bar
        self._spinner = Gtk.Spinner(spinning=True, margin_top=40,
                                     halign=Gtk.Align.CENTER)
        outer.append(self._spinner)

        # Albums strip — populated lazily by _load_albums.
        # Hidden until at least one album exists; horizontal-scroll
        # strip of cards above the photo grid.
        self._albums_label = Gtk.Label(label="Albums", xalign=0)
        self._albums_label.add_css_class("heading")
        self._albums_label.set_margin_start(12)
        self._albums_label.set_margin_top(8)
        self._albums_label.set_visible(False)
        outer.append(self._albums_label)

        self._albums_scroll = Gtk.ScrolledWindow()
        self._albums_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self._albums_scroll.set_kinetic_scrolling(True)
        self._albums_scroll.set_visible(False)
        self._albums_strip = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._albums_strip.set_margin_start(12)
        self._albums_strip.set_margin_end(12)
        self._albums_strip.set_margin_top(4)
        self._albums_strip.set_margin_bottom(8)
        self._albums_scroll.set_child(self._albums_strip)
        outer.append(self._albums_scroll)

        # Scrolled flow-box
        self._flow = Gtk.FlowBox()
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(True)
        self._flow.set_min_children_per_line(3)
        self._flow.set_max_children_per_line(6)
        self._flow.set_column_spacing(4)
        self._flow.set_row_spacing(4)
        self._flow.set_margin_start(8)
        self._flow.set_margin_end(8)
        self._flow.set_margin_top(8)
        self._flow.set_margin_bottom(4)
        outer.append(self._flow)

        # "Load more" button at bottom
        self._load_more_btn = Gtk.Button(label="Load More")
        self._load_more_btn.add_css_class("flat")
        self._load_more_btn.set_margin_top(4)
        self._load_more_btn.set_margin_bottom(12)
        self._load_more_btn.set_halign(Gtk.Align.CENTER)
        self._load_more_btn.set_visible(False)
        self._load_more_btn.connect("clicked", self._load_more)
        outer.append(self._load_more_btn)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(outer)
        self.set_body(scroll)

        self._load_albums()
        self._load_page()

    # ── Albums ──
    def _load_albums(self):
        gid = self._group["id"]
        api = self._api
        def worker():
            albums = api.get_albums(gid)
            GLib.idle_add(self._populate_albums, albums)
        run_in_background(worker)

    def _populate_albums(self, albums: list):
        if not albums:
            return
        self._albums_label.set_visible(True)
        self._albums_scroll.set_visible(True)
        for a in albums:
            self._albums_strip.append(self._make_album_card(a))

    def _make_album_card(self, album: dict) -> Gtk.Widget:
        card = Gtk.Button()
        card.add_css_class("flat")
        card.add_css_class("album-card")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        thumb = Gtk.Picture()
        thumb.set_can_shrink(True)
        thumb.set_content_fit(Gtk.ContentFit.COVER)
        thumb.set_size_request(120, 90)
        thumb.add_css_class("attachment-frame")
        box.append(thumb)

        cover = album.get("cover_image_url", "")
        if cover:
            def _on_cover(path):
                if not path:
                    return
                try:
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        path, 120, 90, False)
                    ok, buf = pix.save_to_bufferv("png", [], [])
                    if ok:
                        thumb.set_paintable(Gdk.Texture.new_from_bytes(
                            GLib.Bytes.new(buf)))
                except Exception:
                    pass
            load_image_async(cover, _on_cover)

        title_lbl = Gtk.Label(label=esc(album.get("title", "Album")))
        title_lbl.set_max_width_chars(15)
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title_lbl)

        imgs   = int(album.get("total_images") or 0)
        videos = int(album.get("total_videos") or 0)
        if imgs or videos:
            parts = []
            if imgs:   parts.append(f"{imgs} image" + ("s" if imgs != 1 else ""))
            if videos: parts.append(f"{videos} video" + ("s" if videos != 1 else ""))
            count_text = " · ".join(parts)
        else:
            count_text = "Empty"
        count_lbl = Gtk.Label(label=count_text)
        count_lbl.add_css_class("dim-label")
        count_lbl.add_css_class("caption")
        box.append(count_lbl)

        card.set_child(box)
        card.connect("clicked",
            lambda *_: AlbumViewDialog(
                self._api, self._group, album, self._parent
            ).present(self._parent))
        return card

    # ── Loading ──
    def _load_page(self, before: str = None):
        if self._loading or self._exhausted:
            return
        self._loading = True
        self._spinner.set_visible(True)
        self._spinner.set_spinning(True)
        self._load_more_btn.set_visible(False)

        def worker():
            msgs = self._api.get_gallery(
                self._group["id"], before=before,
                limit=self.PAGE_SIZE)
            GLib.idle_add(self._on_page, msgs)

        run_in_background(worker)

    def _load_more(self, *_):
        self._load_page(before=self._oldest_ts)

    def _on_page(self, msgs):
        self._loading = False
        self._spinner.set_spinning(False)
        self._spinner.set_visible(False)

        if not msgs:
            if not self._messages:
                empty = Adw.StatusPage(
                    icon_name="image-x-generic-symbolic",
                    title="No Images Yet",
                    description="Images shared in this group will appear here")
                empty.set_vexpand(True)
                # Replace spinner with empty state
                parent_box = self._spinner.get_parent()
                if parent_box:
                    parent_box.remove(self._spinner)
                    parent_box.prepend(empty)
            self._exhausted = True
            return

        for msg in msgs:
            for att in msg.get("attachments", []):
                if att.get("type") == "image":
                    url = att.get("url", "")
                    if url:
                        thumb = self._make_thumb(url, msg)
                        self._flow.append(thumb)

        self._messages.extend(msgs)

        # Track oldest timestamp for pagination
        last = msgs[-1]
        self._oldest_ts = last.get("gallery_ts") or last.get("created_at")

        if len(msgs) >= self.PAGE_SIZE:
            self._load_more_btn.set_label(f"Load More ({len(self._messages)}+ shown)")
            self._load_more_btn.set_visible(True)
        else:
            self._exhausted = True

    # ── Thumbnail ──
    def _make_thumb(self, url: str, msg: dict) -> Gtk.Box:
        container = Gtk.Box()
        container.add_css_class("album-thumb")
        container.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)

        stack = Gtk.Stack()
        spinner = Gtk.Spinner(spinning=True,
                               halign=Gtk.Align.CENTER,
                               valign=Gtk.Align.CENTER)
        stack.add_named(spinner, "loading")

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)
        stack.add_named(picture, "image")

        err = Gtk.Image.new_from_icon_name("image-missing-symbolic")
        stack.add_named(err, "error")
        stack.set_visible_child_name("loading")
        container.append(stack)

        def on_loaded(path):
            if path:
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        path, self.THUMB_SIZE, self.THUMB_SIZE, False)
                    ok, buf = pixbuf.save_to_bufferv("png", [], [])
                    if ok:
                        texture = Gdk.Texture.new_from_bytes(
                            GLib.Bytes.new(buf))
                        picture.set_paintable(texture)
                        stack.set_visible_child_name("image")
                        return
                except Exception:
                    pass
            stack.set_visible_child_name("error")

        load_image_async(url, on_loaded)

        gest = Gtk.GestureClick()
        gest.connect("pressed", lambda *_: self._view_photo(url, msg))
        container.add_controller(gest)
        container.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        return container

    # ── Full-size viewer ──
    def _view_photo(self, url: str, msg: dict):
        sender = msg.get("name", "")
        ts     = msg.get("created_at", 0)
        dt     = datetime.fromtimestamp(ts).strftime("%-d %b %Y, %-I:%M %p") if ts else ""

        viewer = StandardDialog(title=f"{sender}  {dt}".strip(),
                                width=720, height=640)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", lambda *_: self._save_photo(url, viewer))
        viewer.add_header_widget(save_btn, end=True)

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_vexpand(True)

        loading = Gtk.Spinner(spinning=True, margin_top=80,
                               halign=Gtk.Align.CENTER)
        stack = Gtk.Stack()
        stack.add_named(loading, "loading")
        stack.add_named(picture, "image")
        stack.set_visible_child_name("loading")
        stack.set_vexpand(True)

        def on_loaded(path):
            if path:
                try:
                    picture.set_filename(path)
                    stack.set_visible_child_name("image")
                except Exception:
                    pass

        load_image_async(url, on_loaded)
        viewer.set_body(stack)
        viewer.present(self._parent)

    def _save_photo(self, url: str, viewer):
        fd = Gtk.FileDialog()
        fd.set_title("Save Photo")
        fd.set_initial_name("groupme_photo.jpg")
        fd.save(self._parent, None, self._do_save, url)

    def _do_save(self, fd, result, url):
        try:
            f    = fd.save_finish(result)
            dest = f.get_path()
            key  = _cache_key(url)
            src  = CACHE_DIR / f"{key}.img"
            if src.exists():
                import shutil
                shutil.copy(src, dest)
                self._parent.toast("Photo saved!")
        except GLib.Error:
            pass

    # ── Upload ──
    def _pick_photo(self, *_):
        fd = Gtk.FileDialog()
        fd.set_title("Upload Photo to Group")
        ff = Gtk.FileFilter()
        ff.set_name("Images")
        for mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            ff.add_mime_type(mime)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(ff)
        fd.set_filters(store)
        fd.open(self._parent, None, self._on_photo_picked)

    def _on_photo_picked(self, fd, result):
        try:
            f    = fd.open_finish(result)
            path = f.get_path()
        except GLib.Error:
            return

        self._parent.toast("Uploading photo…")

        def worker():
            img_url = self._api.upload_image(path)
            if not img_url:
                GLib.idle_add(lambda: self._parent.toast("Upload failed"))
                return
            # Post as a message with an image attachment to put it in gallery
            msg = self._api.send_message(
                self._group["id"], "", [{"type": "image", "url": img_url}])
            GLib.idle_add(self._after_upload, msg)

        run_in_background(worker)

    def _after_upload(self, msg):
        if msg:
            self._parent.toast("Photo uploaded to group!")
            # Reload gallery from scratch to show new photo
            self._messages  = []
            self._oldest_ts = None
            self._exhausted = False
            child = self._flow.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self._flow.remove(child)
                child = nxt
            self._load_more_btn.set_visible(False)
            self._load_page()
        else:
            self._parent.toast("Failed to upload photo")

    def _on_new_album(self, *_):
        AlbumCreatorDialog(
            self._api, self._group, self._parent).present(self._parent)


# ─────────────────────────── Album Creator ───────────────────────

class AlbumCreatorDialog(StandardDialog):
    """Create a new album in a group's gallery.

    Backed by POST /v3/conversations/{gid}/albums/create. The album
    starts empty; the server auto-fills `cover_image_url` from the
    first media item added afterwards. Adding media is its own flow
    (see api.add_to_album); this dialog only handles creation."""

    def __init__(self, api, group, parent):
        super().__init__(title="New Album", width=380, height=-1)
        self._api    = api
        self._group  = group
        self._parent = parent

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        self.add_header_widget(cancel_btn, end=False)

        self._create_btn = Gtk.Button(label="Create")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.connect("clicked", self._create)
        self.add_header_widget(self._create_btn, end=True)

        box = self.set_scrolled_body(margin=16, spacing=16)

        grp = Adw.PreferencesGroup()
        grp.set_description(
            "Albums collect images and videos from this group's "
            "gallery into a named set.")
        self._name_row = Adw.EntryRow(title="Album name")
        grp.add(self._name_row)
        box.append(grp)

    def _create(self, *_):
        title = self._name_row.get_text().strip()
        if not title:
            self._parent.toast("Album name is required")
            return

        self._create_btn.set_sensitive(False)
        self._create_btn.set_label("Creating…")
        gid = self._group["id"]
        api = self._api

        def worker():
            r = api.create_album(gid, title)
            GLib.idle_add(self._on_done, r, title)
        run_in_background(worker)

    def _on_done(self, album, title):
        self._create_btn.set_sensitive(True)
        self._create_btn.set_label("Create")
        if album:
            self._parent.toast(f"Album '{title}' created")
            self.close()
        else:
            self._parent.toast("Couldn't create album")


# ─────────────────────────── Album View ──────────────────────────

class AlbumViewDialog(StandardDialog):
    """Show the photos and videos in a single album.

    Backed by GET /v3/conversations/{gid}/albums/{aid}/media. Each
    media item has a media_url and a media_type ('image' or 'video').
    Tapping a thumbnail opens it in the appropriate viewer."""

    THUMB_SIZE = 140

    def __init__(self, api, group, album, parent):
        super().__init__(title=album.get("title", "Album"),
                         width=520, height=560)
        self._api    = api
        self._group  = group
        self._album  = album
        self._parent = parent

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._spinner = Gtk.Spinner(spinning=True, margin_top=40,
                                     halign=Gtk.Align.CENTER)
        outer.append(self._spinner)

        self._flow = Gtk.FlowBox()
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(True)
        self._flow.set_min_children_per_line(3)
        self._flow.set_max_children_per_line(6)
        self._flow.set_column_spacing(4)
        self._flow.set_row_spacing(4)
        self._flow.set_margin_start(8); self._flow.set_margin_end(8)
        self._flow.set_margin_top(8);   self._flow.set_margin_bottom(8)
        outer.append(self._flow)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(outer)
        self.set_body(scroll)

        self._load()

    def _load(self):
        gid = self._group["id"]
        aid = self._album["album_id"]
        api = self._api
        def worker():
            full = api.get_album(gid, aid)
            atts = (full or {}).get("attachments") or []
            GLib.idle_add(self._populate, atts)
        run_in_background(worker)

    def _populate(self, attachments: list):
        self._spinner.set_spinning(False)
        self._spinner.set_visible(False)

        if not attachments:
            empty = Adw.StatusPage(
                icon_name="image-x-generic-symbolic",
                title="Album is empty",
                description="No photos or videos yet.")
            empty.set_vexpand(True)
            parent_box = self._spinner.get_parent()
            if parent_box:
                parent_box.remove(self._spinner)
                parent_box.prepend(empty)
            return

        for att in attachments:
            url = att.get("media_url", "")
            kind = att.get("media_type", "image")
            if not url:
                continue
            self._flow.append(self._make_thumb(url, kind, att))

    def _make_thumb(self, url: str, kind: str, att: dict) -> Gtk.Widget:
        container = Gtk.Overlay()
        container.add_css_class("album-thumb")
        container.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)

        stack = Gtk.Stack()
        spinner = Gtk.Spinner(spinning=True,
                               halign=Gtk.Align.CENTER,
                               valign=Gtk.Align.CENTER)
        stack.add_named(spinner, "loading")

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)
        stack.add_named(picture, "image")

        err = Gtk.Image.new_from_icon_name("image-missing-symbolic")
        stack.add_named(err, "error")
        stack.set_visible_child_name("loading")
        container.set_child(stack)

        if kind == "video":
            play_overlay = Gtk.Image.new_from_icon_name(
                "media-playback-start-symbolic")
            play_overlay.set_pixel_size(40)
            play_overlay.set_halign(Gtk.Align.CENTER)
            play_overlay.set_valign(Gtk.Align.CENTER)
            play_overlay.add_css_class("video-play-overlay")
            container.add_overlay(play_overlay)

        # Thumbnail source: images use the media_url directly; videos
        # use preview_url if the server gave one (often empty for
        # GroupMe albums, in which case we fall through to the error
        # icon since we can't decode a thumbnail from the video URL
        # without playback).
        thumb_src = url if kind == "image" else att.get("preview_url", "")

        def on_loaded(path):
            if path:
                try:
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        path, self.THUMB_SIZE, self.THUMB_SIZE, False)
                    ok, buf = pix.save_to_bufferv("png", [], [])
                    if ok:
                        picture.set_paintable(
                            Gdk.Texture.new_from_bytes(
                                GLib.Bytes.new(buf)))
                        stack.set_visible_child_name("image")
                        return
                except Exception:
                    pass
            stack.set_visible_child_name("error")

        if thumb_src:
            load_image_async(thumb_src, on_loaded)
        else:
            stack.set_visible_child_name("error")

        gest = Gtk.GestureClick()
        gest.connect("pressed",
            lambda *_: self._open_item(url, kind, att))
        container.add_controller(gest)
        container.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        return container

    def _open_item(self, url: str, kind: str, att: dict):
        if kind == "video":
            viewer = StandardDialog(title="Video", width=720, height=480)
            video = VideoAttachment(
                url, att.get("preview_url", ""), self._parent)
            viewer.set_body(video)
            viewer.present(self._parent)
            return

        # Image — fullsize viewer with scrolled Gtk.Picture.
        viewer = StandardDialog(title="Image", width=720, height=640)
        viewer.set_follows_content_size(False)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True); scroll.set_hexpand(True)
        scroll.set_kinetic_scrolling(True)

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_vexpand(True); picture.set_hexpand(True)

        cached = CACHE_DIR / f"{_cache_key(url)}.img"
        if cached.exists():
            picture.set_filename(str(cached))
        else:
            def _on_loaded(path):
                if path:
                    try: picture.set_filename(path)
                    except Exception: pass
            load_image_async(url, _on_loaded)

        scroll.set_child(picture)
        viewer.set_body(scroll)
        viewer.present(self._parent)

