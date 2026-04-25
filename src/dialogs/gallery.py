"""Banter — GalleryDialog (group image gallery)."""

import shutil
from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf

from ..constants import dbg, esc, CACHE_DIR
from ..async_utils import run_in_background
from ..helpers import load_image_async, _cache_key


class GalleryDialog(Adw.Dialog):
    """Browse all images sent in a group (the real GroupMe gallery API).

    GroupMe has no concept of separate albums via its public API.
    The gallery endpoint returns all messages that contain images,
    paginated by timestamp newest-first.
    """

    PAGE_SIZE = 100
    THUMB_SIZE = 120

    def __init__(self, api, group, parent):
        super().__init__()
        self._api        = api
        self._group      = group
        self._parent     = parent
        self._messages   = []          # accumulated message dicts
        self._oldest_ts  = None        # gallery_ts of oldest fetched msg
        self._loading    = False
        self._exhausted  = False

        self.set_title(f"Gallery – {group.get('name','')}")
        self.set_content_width(520)
        self.set_content_height(660)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Upload button in header
        up_btn = Gtk.Button(icon_name="document-send-symbolic")
        up_btn.add_css_class("flat")
        up_btn.set_tooltip_text("Upload photo to group")
        up_btn.connect("clicked", self._pick_photo)
        hdr.pack_end(up_btn)

        # Spinner bar
        self._spinner = Gtk.Spinner(spinning=True, margin_top=40,
                                     halign=Gtk.Align.CENTER)
        outer.append(self._spinner)

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
        tv.set_content(scroll)
        self.set_child(tv)

        self._load_page()

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
        viewer = Adw.Dialog()
        sender = msg.get("name", "")
        ts     = msg.get("created_at", 0)
        dt     = datetime.fromtimestamp(ts).strftime("%-d %b %Y, %-I:%M %p") if ts else ""
        viewer.set_title(f"{sender}  {dt}".strip())
        viewer.set_content_width(720)
        viewer.set_content_height(640)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", lambda *_: self._save_photo(url, viewer))
        hdr.pack_end(save_btn)
        tv.add_top_bar(hdr)

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
        tv.set_content(stack)
        viewer.set_child(tv)
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


# ─────────────────────────── Create Event Dialog ─────────────────

