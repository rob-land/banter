"""Banter — miscellaneous reusable widgets (LoadingRow, ImageAttachment, FileAttachment, DateSeparator)."""

from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, GdkPixbuf, Gdk, Gio

from ..constants import CACHE_DIR
from ..async_utils import run_in_background
from ..helpers import load_image_async, _cache_key
from .base import StandardDialog


class LoadingRow(Adw.ActionRow):
    def __init__(self, label="Loading…"):
        super().__init__(title=label)
        spinner = Gtk.Spinner(spinning=True)
        self.add_suffix(spinner)
        self.set_activatable(False)


class ImageAttachment(Gtk.Frame):
    """Lazy-loading image widget for message attachments."""
    MAX_W, MAX_H = 280, 200

    def __init__(self, url: str, parent_window):
        super().__init__()
        self.add_css_class("attachment-frame")
        self.parent_window = parent_window
        self._url = url

        self._stack = Gtk.Stack()
        spinner = Gtk.Spinner(spinning=True, margin_top=16,
                               margin_bottom=16, margin_start=16,
                               margin_end=16)
        self._stack.add_named(spinner, "loading")

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_size_request(self.MAX_W, self.MAX_H)
        self._stack.add_named(self._picture, "image")

        err = Gtk.Label(label="⚠ Image unavailable")
        err.add_css_class("dim-label")
        self._stack.add_named(err, "error")

        self._stack.set_visible_child_name("loading")
        self.set_child(self._stack)

        self.set_size_request(self.MAX_W, self.MAX_H)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        gest = Gtk.GestureClick()
        gest.connect("pressed", self._on_click)
        self.add_controller(gest)

        load_image_async(url, self._on_loaded)

    def _on_loaded(self, path):
        if path:
            try:
                # Scale the image then encode to PNG bytes so we can build
                # a Gdk.Texture from the in-memory buffer.
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, self.MAX_W, self.MAX_H, True)
                ok, buf = pixbuf.save_to_bufferv("png", [], [])
                if ok:
                    texture = Gdk.Texture.new_from_bytes(
                        GLib.Bytes.new(buf))
                    self._picture.set_paintable(texture)
                    self._stack.set_visible_child_name("image")
                    return
            except Exception:
                pass
        self._stack.set_visible_child_name("error")

    def _on_click(self, gest, n, x, y):
        dialog = StandardDialog(title="Image", width=720, height=640)
        dialog.set_follows_content_size(False)

        save_btn = Gtk.Button(icon_name="document-save-symbolic")
        save_btn.set_tooltip_text("Save full-size image")
        save_btn.add_css_class("flat")
        save_btn.connect("clicked", self._save_image, dialog)
        dialog.add_header_widget(save_btn, end=True)

        # Scrolled container lets the user pan very large images and
        # makes the dialog behave sensibly on narrow phone screens.
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_kinetic_scrolling(True)

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_vexpand(True)
        picture.set_hexpand(True)

        cached = CACHE_DIR / f"{_cache_key(self._url)}.img"
        if cached.exists():
            picture.set_filename(str(cached))

        scroll.set_child(picture)
        dialog.set_body(scroll)
        dialog.present(self.parent_window)

    def _save_image(self, btn, dialog):
        fd = Gtk.FileDialog()
        fd.set_title("Save Image")
        # Guess an extension from the URL so the default filename is sensible
        ext = "jpg"
        for e in ("png", "gif", "webp", "jpeg", "jpg"):
            if f".{e}" in self._url.lower():
                ext = "jpeg" if e == "jpg" else e
                break
        fd.set_initial_name(f"groupme_image.{ext}")
        fd.save(self.parent_window, None, self._do_save)

    def _do_save(self, fd, result):
        try:
            file = fd.save_finish(result)
            dest = file.get_path()
            key  = _cache_key(self._url)
            src  = CACHE_DIR / f"{key}.img"
            if src.exists():
                import shutil
                shutil.copy(src, dest)
        except GLib.Error:
            pass


def _format_size(n: int) -> str:
    """Pretty-print a byte count: 18 KB / 4.2 MB / etc."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    if i == 0:
        return f"{int(f)} B"
    return f"{f:.0f} {units[i]}" if f >= 10 else f"{f:.1f} {units[i]}"


class FileAttachment(Gtk.Button):
    """Pill-style download button for a `{type:file, file_id}` attachment.

    Async-fetches `{file_name, file_size, mime_type}` from the
    fileData endpoint on construction; until that resolves the label
    shows a generic placeholder. Clicking opens the file's download
    URL through the system handler — Banter doesn't manage the
    download itself."""

    def __init__(self, file_id: str, conv_id: str, api, parent_window):
        super().__init__()
        self.add_css_class("file-attachment")
        self.add_css_class("flat")
        self._file_id  = file_id
        # The conversation_id used by file.groupme.com endpoints —
        # group_id for groups, "<lo>+<hi>" for DMs.
        self._cid      = conv_id
        self._api      = api
        self._parent   = parent_window
        self._file_name = ""

        box = Gtk.Box(spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
        icon.set_pixel_size(22)
        box.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._name_lbl = Gtk.Label(label="Loading…", xalign=0)
        self._name_lbl.set_ellipsize(3)   # END
        self._name_lbl.set_max_width_chars(28)
        self._size_lbl = Gtk.Label(label="", xalign=0)
        self._size_lbl.add_css_class("dim-label")
        self._size_lbl.add_css_class("caption")
        text_box.append(self._name_lbl)
        text_box.append(self._size_lbl)
        box.append(text_box)
        self.set_child(box)

        self.connect("clicked", self._on_clicked)
        self._fetch_metadata()

    def _fetch_metadata(self):
        cid, fid = self._cid, self._file_id
        api = self._api
        def worker():
            data = api.get_file_data(cid, [fid])
            GLib.idle_add(self._on_metadata, data.get(fid) or {})
        run_in_background(worker)

    def _on_metadata(self, fd: dict):
        name = fd.get("file_name") or "Attachment"
        size = fd.get("file_size") or 0
        self._file_name = name
        self._name_lbl.set_text(name)
        size_text = _format_size(size)
        if size_text:
            self._size_lbl.set_text(size_text)

    def _on_clicked(self, *_):
        """Tap → save dialog → authenticated download to chosen path.
        Keeps the user in-app; the alternative (handing the URL to
        Gio.AppInfo.launch_default_for_uri) ships them out to the
        system browser, which works but breaks flow."""
        fd = Gtk.FileDialog()
        fd.set_title("Save attachment")
        fd.set_initial_name(self._file_name or "groupme_file")
        fd.save(self._parent, None, self._on_save_chosen)

    def _on_save_chosen(self, fd, result):
        try:
            f    = fd.save_finish(result)
            dest = f.get_path()
        except GLib.Error:
            return   # user cancelled

        name = self._file_name or "file"
        try: self._parent.toast(f"Saving {name}…")
        except Exception: pass

        cid    = self._cid
        fid    = self._file_id
        api    = self._api
        parent = self._parent

        def worker():
            ok = api.download_file(cid, fid, dest)
            def report():
                try:
                    parent.toast(
                        f"Saved to {dest}" if ok else "Download failed")
                except Exception: pass
            GLib.idle_add(report)

        run_in_background(worker)


# ─────────────────────────── Date Separator ──────────────────────

class DateSeparator(Gtk.Box):
    """Centered pill label shown between messages on different calendar days."""

    def __init__(self, d):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class("date-separator")
        self.set_halign(Gtk.Align.CENTER)

        text = self._format(d)
        lbl  = Gtk.Label(label=text)
        lbl.add_css_class("date-separator-label")
        self.append(lbl)

    @staticmethod
    def _format(d) -> str:
        today     = datetime.now().date()
        yesterday = today.__class__.fromordinal(today.toordinal() - 1)
        if d == today:
            return "Today"
        if d == yesterday:
            return "Yesterday"
        # Within the current year: "Monday, January 6"
        if d.year == today.year:
            return d.strftime("%A, %B %-d")
        # Older: "January 6, 2023"
        return d.strftime("%B %-d, %Y")


# ─────────────────────────── Message Bubble ──────────────────────

