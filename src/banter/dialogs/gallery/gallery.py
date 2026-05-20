"""GalleryDialog — browse all images shared in a group."""

import shutil
from datetime import datetime

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from ...async_utils import run_in_background
from ...constants import CACHE_DIR, esc
from ...helpers import _cache_key, load_image_async
from ...widgets.base import StandardDialog
from ...widgets.misc import VideoAttachment
from ._helpers import _do_bulk_download, _safe_fs_name
from .album_creator import AlbumCreatorDialog
from .album_picker import AlbumPickerDialog
from .album_view import AlbumViewDialog


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
        self._api = api
        self._group = group
        self._parent = parent
        self._messages = []          # accumulated message dicts
        self._albums = []            # populated by _populate_albums
        self._oldest_ts = None       # gallery_ts of oldest fetched msg
        self._loading = False
        self._exhausted = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Upload button in header (rightmost)
        up_btn = Gtk.Button(icon_name="document-send-symbolic")
        up_btn.add_css_class("flat")
        up_btn.set_tooltip_text("Upload photo to group")
        up_btn.connect("clicked", self._pick_photo)
        self.add_header_widget(up_btn, end=True)

        # Download-everything button (left of upload)
        dl_btn = Gtk.Button(icon_name="document-save-symbolic")
        dl_btn.add_css_class("flat")
        dl_btn.set_tooltip_text(
            "Download all loaded photos / videos and every album")
        dl_btn.connect("clicked", self._on_download_all)
        self.add_header_widget(dl_btn, end=True)

        # New album button (left of download)
        new_album_btn = Gtk.Button(icon_name="folder-new-symbolic")
        new_album_btn.add_css_class("flat")
        new_album_btn.set_tooltip_text("Create new album")
        new_album_btn.connect("clicked", self._on_new_album)
        self.add_header_widget(new_album_btn, end=True)

        # Multi-select toggle. Drives _make_thumb's click branching
        # and toggles FlowBox selection mode for visual feedback.
        self._select_mode = False
        self._select_btn = Gtk.ToggleButton()
        self._select_btn.set_icon_name("object-select-symbolic")
        self._select_btn.add_css_class("flat")
        self._select_btn.set_tooltip_text("Select photos to add to an album")
        self._select_btn.connect("toggled", self._on_select_toggle)
        self.add_header_widget(self._select_btn, end=True)

        # Bottom action bar — only revealed when at least one
        # thumbnail is selected.
        self._select_count_lbl = Gtk.Label()
        self._select_count_lbl.add_css_class("dim-label")
        self._add_to_album_btn = Gtk.Button(label="Add to Album…")
        self._add_to_album_btn.add_css_class("suggested-action")
        self._add_to_album_btn.connect("clicked", self._on_add_to_album)
        self._action_bar = Gtk.ActionBar()
        self._action_bar.pack_start(self._select_count_lbl)
        self._action_bar.pack_end(self._add_to_album_btn)
        self._action_bar.set_revealed(False)
        self.add_bottom_bar(self._action_bar)

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
        self._albums = list(albums or [])
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
        thumb.set_content_fit(Gtk.ContentFit.CONTAIN)
        thumb.set_size_request(120, 90)
        thumb.add_css_class("attachment-frame")
        thumb.add_css_class("album-thumb")
        box.append(thumb)

        cover = album.get("cover_image_url", "")
        if cover:
            def _on_cover(path):
                if not path:
                    return
                try:
                    # preserve_aspect_ratio=True so the texture itself
                    # is not distorted; Picture's CONTAIN then letter/
                    # pillarboxes it inside the 120×90 frame.
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        path, 120, 90, True)
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

        imgs = int(album.get("total_images") or 0)
        videos = int(album.get("total_videos") or 0)
        if imgs or videos:
            parts = []
            if imgs:
                parts.append(f"{imgs} image" + ("s" if imgs != 1 else ""))
            if videos:
                parts.append(f"{videos} video" + ("s" if videos != 1 else ""))
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
                    title="Nothing here yet",
                    description="Images and videos shared in this group will appear here")
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
                if att.get("type") in ("image", "video") and att.get("url"):
                    self._flow.append(self._make_thumb(att, msg))

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
    def _make_thumb(self, att: dict, msg: dict) -> Gtk.Widget:
        kind = att.get("type", "image")
        url = att.get("url", "")
        preview_url = att.get("preview_url", "")

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
        # Preserve aspect ratio inside the square cell — landscape
        # photos letterbox top/bottom, portrait pillarboxes left/right.
        # The .album-thumb backdrop fills the bars.
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)
        stack.add_named(picture, "image")

        # For videos without a preview URL (common in /gallery responses)
        # we'd otherwise show "image missing" — but the play overlay
        # makes it clear it's still playable, so use a video glyph
        # instead of the error icon as the placeholder.
        ph_icon = "video-x-generic-symbolic" if kind == "video" \
                  else "image-missing-symbolic"
        err = Gtk.Image.new_from_icon_name(ph_icon)
        err.set_pixel_size(48)
        stack.add_named(err, "error")
        stack.set_visible_child_name("loading")
        container.set_child(stack)

        if kind == "video":
            play = Gtk.Image.new_from_icon_name(
                "media-playback-start-symbolic")
            play.set_pixel_size(40)
            play.set_halign(Gtk.Align.CENTER)
            play.set_valign(Gtk.Align.CENTER)
            play.add_css_class("video-play-overlay")
            container.add_overlay(play)

        def on_loaded(path):
            if path:
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        path, self.THUMB_SIZE, self.THUMB_SIZE, True)
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

        # Images use the URL directly; videos use the preview_url when
        # the server provides one. Without a preview_url, jump straight
        # to the placeholder — we can't decode a frame from the video
        # URL without playback.
        thumb_src = url if kind == "image" else preview_url
        if thumb_src:
            load_image_async(thumb_src, on_loaded)
        else:
            stack.set_visible_child_name("error")

        # Stash url + kind so the picker can build the add_to_album
        # payload without re-walking the message list.
        container._gallery_url = url
        container._gallery_kind = kind

        def on_click(*_):
            if self._select_mode:
                child = container.get_parent()  # FlowBoxChild
                if child.is_selected():
                    self._flow.unselect_child(child)
                else:
                    self._flow.select_child(child)
                self._update_action_bar()
            elif kind == "video":
                self._view_video(url, preview_url, msg)
            else:
                self._view_photo(url, msg)

        gest = Gtk.GestureClick()
        gest.connect("pressed", on_click)
        container.add_controller(gest)
        container.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        return container

    # ── Full-size viewer ──
    def _view_video(self, url: str, preview_url: str, msg: dict):
        sender = msg.get("name", "")
        ts = msg.get("created_at", 0)
        dt = datetime.fromtimestamp(ts).strftime("%-d %b %Y, %-I:%M %p") if ts else ""
        viewer = StandardDialog(title=f"{sender}  {dt}".strip(),
                                width=720, height=480)
        video = VideoAttachment(url, preview_url, self._parent)
        viewer.set_body(video)
        viewer.present(self._parent)

    def _view_photo(self, url: str, msg: dict):
        sender = msg.get("name", "")
        ts = msg.get("created_at", 0)
        dt = datetime.fromtimestamp(ts).strftime("%-d %b %Y, %-I:%M %p") if ts else ""

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
            f = fd.save_finish(result)
            dest = f.get_path()
            key = _cache_key(url)
            src = CACHE_DIR / f"{key}.img"
            if src.exists():
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
            f = fd.open_finish(result)
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
            self._messages = []
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

    # ── Download everything ──
    def _on_download_all(self, *_):
        if not self._messages and not self._albums:
            self._parent.toast("Nothing to download yet")
            return
        fd = Gtk.FileDialog()
        fd.set_title(
            f"Download '{self._group.get('name', 'Gallery')}'")
        fd.select_folder(self._parent, None, self._on_download_all_dest)

    def _on_download_all_dest(self, fd, result):
        try:
            folder = fd.select_folder_finish(result)
        except GLib.Error:
            return
        dest = folder.get_path()
        if not dest:
            return

        # Snapshot the state we'll need off the main thread.
        messages = list(self._messages)
        albums = list(self._albums)
        api = self._api
        gid = self._group["id"]
        parent = self._parent

        parent.toast("Preparing download…")

        def worker():
            items = []
            # Currently-loaded gallery photos / videos.
            for msg in messages:
                for att in msg.get("attachments", []):
                    if att.get("type") in ("image", "video") and att.get("url"):
                        items.append({
                            "url":    att["url"],
                            "kind":   att["type"],
                            "subdir": "gallery",
                        })
            # Each album's media — one round-trip per album.
            for album in albums:
                aid = album.get("album_id")
                if not aid:
                    continue
                title = (album.get("title") or "album").strip() or "album"
                safe = _safe_fs_name(title)
                try:
                    full = api.get_album(gid, aid)
                except Exception:
                    continue
                for att in (full or {}).get("attachments") or []:
                    url = att.get("media_url", "")
                    kind = att.get("media_type", "image")
                    if url:
                        items.append({
                            "url":    url,
                            "kind":   kind,
                            "subdir": f"albums/{safe}",
                        })
            _do_bulk_download(items, dest, parent)

        run_in_background(worker)

    # ── Multi-select / "Add to album" ──
    def _on_select_toggle(self, btn):
        self._select_mode = btn.get_active()
        if self._select_mode:
            self._flow.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        else:
            self._flow.unselect_all()
            self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._update_action_bar()

    def _update_action_bar(self):
        n = len(self._flow.get_selected_children())
        if self._select_mode and n > 0:
            self._select_count_lbl.set_text(
                f"{n} selected" if n != 1 else "1 selected")
            self._action_bar.set_revealed(True)
        else:
            self._action_bar.set_revealed(False)

    def _on_add_to_album(self, *_):
        media = []
        for child in self._flow.get_selected_children():
            container = child.get_child()
            url = getattr(container, "_gallery_url", "")
            kind = getattr(container, "_gallery_kind", "image")
            if url:
                media.append({
                    "media_url":    url,
                    "media_type":   kind,
                    "media_source": "album",
                })
        if not media:
            return
        AlbumPickerDialog(
            self._api, self._group, media, self._parent,
            on_done=self._on_picker_done,
        ).present(self._parent)

    def _on_picker_done(self, ok: bool):
        if not ok:
            return
        # Exit select mode and clear the action bar; the user will
        # likely want to keep browsing.
        self._select_btn.set_active(False)
