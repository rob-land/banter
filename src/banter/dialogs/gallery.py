"""Banter — GalleryDialog (group image gallery)."""

import shutil
from datetime import datetime
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
        self._albums     = []          # populated by _populate_albums
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
        kind        = att.get("type", "image")
        url         = att.get("url", "")
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
        container._gallery_url  = url
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
        ts     = msg.get("created_at", 0)
        dt     = datetime.fromtimestamp(ts).strftime("%-d %b %Y, %-I:%M %p") if ts else ""
        viewer = StandardDialog(title=f"{sender}  {dt}".strip(),
                                width=720, height=480)
        video = VideoAttachment(url, preview_url, self._parent)
        viewer.set_body(video)
        viewer.present(self._parent)

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
        albums   = list(self._albums)
        api      = self._api
        gid      = self._group["id"]
        parent   = self._parent

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
            url  = getattr(container, "_gallery_url", "")
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


# ─────────────────────────── Album Creator ───────────────────────

class AlbumCreatorDialog(StandardDialog):
    """Create a new album in a group's gallery.

    Backed by POST /v3/conversations/{gid}/albums/create. The album
    starts empty; the server auto-fills `cover_image_url` from the
    first media item added afterwards. Adding media is its own flow
    (see api.add_to_album); this dialog only handles creation.

    Pass `on_created` to chain a follow-up: the callback receives the
    new album dict on success and runs after the dialog closes."""

    def __init__(self, api, group, parent, on_created=None):
        super().__init__(title="New Album", width=380, height=-1)
        self._api        = api
        self._group      = group
        self._parent     = parent
        self._on_created = on_created

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
            if callable(self._on_created):
                self._on_created(album)
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
        self._flow.set_margin_start(8); self._flow.set_margin_end(8)
        self._flow.set_margin_top(8);   self._flow.set_margin_bottom(8)
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
            try: self._outer.remove(self._empty_state)
            except Exception: pass
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


# ─────────────────────────── Album Picker ────────────────────────

class AlbumPickerDialog(StandardDialog):
    """Pick a destination album for a set of media items, or create a
    new one. The flow:

      1. Lists existing albums via api.get_albums.
      2. Tap an album → POST those items via api.add_to_album.
      3. Tap "New album…" → opens AlbumCreatorDialog; when that
         dialog reports a successfully-created album, the picker's
         add_to_album follow-up runs automatically.

    `on_done(ok: bool)` fires once after a successful add so the
    caller can clear selection state."""

    def __init__(self, api, group, media: list, parent, on_done=None):
        super().__init__(title="Add to Album", width=420, height=520)
        self._api     = api
        self._group   = group
        self._media   = media
        self._parent  = parent
        self._on_done = on_done

        body = self.set_scrolled_body(margin=12, spacing=12)

        n = len(media)
        hint = Gtk.Label(
            label=f"Adding {n} item{'s' if n != 1 else ''} to:",
            xalign=0)
        hint.add_css_class("dim-label")
        body.append(hint)

        self._spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        self._spinner.set_margin_top(20)
        body.append(self._spinner)

        self._list_grp = Adw.PreferencesGroup()
        self._list_grp.set_visible(False)
        body.append(self._list_grp)

        new_grp = Adw.PreferencesGroup()
        new_row = Adw.ButtonRow(title="New album…",
                                 start_icon_name="folder-new-symbolic")
        new_row.connect("activated", self._on_new_album)
        new_grp.add(new_row)
        body.append(new_grp)

        self._load_albums()

    def _load_albums(self):
        gid = self._group["id"]
        api = self._api
        def worker():
            albums = api.get_albums(gid)
            GLib.idle_add(self._populate, albums or [])
        run_in_background(worker)

    def _populate(self, albums: list):
        self._spinner.set_spinning(False)
        self._spinner.set_visible(False)
        self._list_grp.set_visible(True)

        if not albums:
            empty = Adw.ActionRow(title="No albums yet",
                                   subtitle="Use \"New album…\" below to create one.")
            empty.set_activatable(False)
            self._list_grp.add(empty)
            return

        for a in albums:
            row = Adw.ActionRow(title=esc(a.get("title", "Album")))
            imgs   = int(a.get("total_images") or 0)
            videos = int(a.get("total_videos") or 0)
            counts = []
            if imgs:   counts.append(f"{imgs} image" + ("s" if imgs != 1 else ""))
            if videos: counts.append(f"{videos} video" + ("s" if videos != 1 else ""))
            row.set_subtitle(" · ".join(counts) if counts else "Empty")
            row.set_activatable(True)
            row.connect("activated", self._add_to_existing, a)
            self._list_grp.add(row)

    def _add_to_existing(self, _row, album: dict):
        self._do_add(album)

    def _on_new_album(self, _row):
        # Hand off to the creator dialog with a follow-up that reuses
        # _do_add against the freshly-created album. Closing the
        # picker first avoids stacking three dialogs.
        media   = self._media
        on_done = self._on_done
        api     = self._api
        group   = self._group
        parent  = self._parent

        def after_create(album):
            _add_media(api, group, album, media, parent, on_done)

        self.close()
        AlbumCreatorDialog(
            api, group, parent, on_created=after_create
        ).present(parent)

    def _do_add(self, album: dict):
        _add_media(self._api, self._group, album, self._media,
                   self._parent, self._on_done)
        self.close()


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

    aid   = album.get("album_id") or album.get("id") or ""
    title = album.get("title") or "album"

    def on_picked(_fd, result):
        try:
            f    = fd.open_finish(result)
            path = f.get_path()
        except GLib.Error:
            return   # user cancelled
        try: parent.toast("Uploading photo…")
        except Exception: pass

        def worker():
            url = api.upload_image(path)
            if not url:
                GLib.idle_add(lambda: _toast(parent, "Upload failed"))
                return
            media = [{"media_url":    url,
                       "media_type":   "image",
                       "media_source": "album"}]
            r  = api.add_to_album(gid, aid, media)
            ok = r is not None
            def report():
                _toast(parent, f"Added to '{title}'" if ok else "Add failed")
                if ok and callable(on_done):
                    try: on_done()
                    except Exception: pass
            GLib.idle_add(report)

        run_in_background(worker)

    fd.open(parent, None, on_picked)


def _toast(parent, text):
    try: parent.toast(text)
    except Exception: pass


def _add_media(api, group, album, media, parent, on_done):
    """Shared "POST media to this album" worker used by both the
    existing-album and new-album flows."""
    gid   = group["id"]
    aid   = album["album_id"]
    title = album.get("title", "album")
    n     = len(media)

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


# ─────────────────────────── Bulk download ────────────────────────

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")


def _safe_fs_name(name: str) -> str:
    """Strip / replace characters that are awkward in filesystem paths.
    Falls back to a generic label if everything's stripped."""
    import re
    cleaned = re.sub(r'[\/\\:\*\?"<>\|\x00-\x1f]', '_', name).strip(" .")
    return cleaned[:80] or "album"


def _ext_for(url: str, kind: str) -> str:
    """Infer a sensible file extension. GroupMe URLs often end with a
    size suffix like '.jpg.large' or '.png.preview'; scan the path
    for the first known media extension instead of taking the tail."""
    from urllib.parse import urlsplit
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
        try: parent.toast("Nothing to download")
        except Exception: pass
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
    import shutil
    import urllib.request
    from pathlib import Path

    from ..constants import APP_VERSION

    dest = Path(dest_root)
    # Counter per subdir for non-colliding filenames.
    seq: dict = {}
    saved = 0

    for item in items:
        url    = item.get("url", "")
        kind   = item.get("kind", "image")
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

