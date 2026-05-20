"""AlbumViewDialog — show the contents of a single album."""

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

from ...async_utils import run_in_background
from ...constants import CACHE_DIR
from ...helpers import _cache_key, load_image_async
from ...widgets.base import StandardDialog
from ...widgets.misc import VideoAttachment
from ._helpers import _bulk_download, _safe_fs_name, add_local_image_to_album


class AlbumViewDialog(StandardDialog):
    """Show the photos and videos in a single album.

    Backed by GET /v3/conversations/{gid}/albums/{aid}/media. Each
    media item has a media_url and a media_type ('image' or 'video').
    Tapping a thumbnail opens it in the appropriate viewer."""

    THUMB_SIZE = 140

    def __init__(self, api, group, album, parent):
        super().__init__(title=album.get("title", "Album"),
                         width=520, height=560)
        self._api = api
        self._group = group
        self._album = album
        self._parent = parent
        # Tracked so _load can clean up the empty-state placeholder
        # when refreshing after the first photo is added.
        self._empty_state = None
        # Captured from _populate so the download button doesn't have
        # to re-fetch the album.
        self._attachments: list = []

        # Download-album button (left of the add button).
        dl_btn = Gtk.Button(icon_name="document-save-symbolic")
        dl_btn.add_css_class("flat")
        dl_btn.set_tooltip_text("Download every photo / video in this album")
        dl_btn.connect("clicked", self._on_download_album)
        self.add_header_widget(dl_btn, end=True)
        self._download_btn = dl_btn

        # "+" button in the header — picks a local image, uploads,
        # adds, then re-loads the flow so the new tile appears.
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.add_css_class("flat")
        add_btn.set_tooltip_text("Add a photo to this album")
        add_btn.connect("clicked", self._on_add_clicked)
        self.add_header_widget(add_btn, end=True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._outer = outer

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
        self._flow.set_margin_start(8)
        self._flow.set_margin_end(8)
        self._flow.set_margin_top(8)
        self._flow.set_margin_bottom(8)
        outer.append(self._flow)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(outer)
        self.set_body(scroll)

        self._load()

    def _on_add_clicked(self, *_):
        add_local_image_to_album(
            self._api, self._group["id"], self._album,
            self._parent, on_done=self._refresh_after_add)

    def _refresh_after_add(self):
        """Clear the current tiles + empty state and re-fetch. Cheap
        — the album has at most a few dozen items in practice."""
        # Drop existing tiles
        child = self._flow.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._flow.remove(child)
            child = nxt
        # Drop empty-state placeholder if it's there
        if self._empty_state is not None:
            try:
                self._outer.remove(self._empty_state)
            except Exception:
                pass
            self._empty_state = None
        # Show the spinner again and re-fetch
        self._spinner.set_visible(True)
        self._spinner.set_spinning(True)
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

        self._attachments = list(attachments or [])

        if not attachments:
            self._download_btn.set_sensitive(False)
            empty = Adw.StatusPage(
                icon_name="image-x-generic-symbolic",
                title="Album is empty",
                description="No photos or videos yet.")
            empty.set_vexpand(True)
            parent_box = self._spinner.get_parent()
            if parent_box:
                parent_box.remove(self._spinner)
                parent_box.prepend(empty)
            self._empty_state = empty
            return

        self._download_btn.set_sensitive(True)
        for att in attachments:
            url = att.get("media_url", "")
            kind = att.get("media_type", "image")
            if not url:
                continue
            self._flow.append(self._make_thumb(url, kind, att))

    # ── Download album ──
    def _on_download_album(self, *_):
        if not self._attachments:
            self._parent.toast("Nothing to download")
            return
        title = (self._album.get("title") or "album").strip() or "album"
        safe = _safe_fs_name(title)
        items = [
            {"url": att.get("media_url", ""),
             "kind": att.get("media_type", "image"),
             "subdir": safe}
            for att in self._attachments
            if att.get("media_url")
        ]
        if not items:
            self._parent.toast("Nothing to download")
            return
        fd = Gtk.FileDialog()
        fd.set_title(f"Download '{title}'")
        fd.select_folder(self._parent, None,
                         lambda fd2, res: self._on_download_dest(
                             fd2, res, items))

    def _on_download_dest(self, fd, result, items):
        try:
            folder = fd.select_folder_finish(result)
        except GLib.Error:
            return
        dest = folder.get_path()
        if not dest:
            return
        _bulk_download(items, dest, self._parent)

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
        # Preserve aspect ratio (letterbox / pillarbox) — the
        # .album-thumb backdrop fills the bars.
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
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
                        path, self.THUMB_SIZE, self.THUMB_SIZE, True)
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
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_kinetic_scrolling(True)

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_vexpand(True)
        picture.set_hexpand(True)

        cached = CACHE_DIR / f"{_cache_key(url)}.img"
        if cached.exists():
            picture.set_filename(str(cached))
        else:
            def _on_loaded(path):
                if path:
                    try:
                        picture.set_filename(path)
                    except Exception:
                        pass
            load_image_async(url, _on_loaded)

        scroll.set_child(picture)
        viewer.set_body(scroll)
        viewer.present(self._parent)
