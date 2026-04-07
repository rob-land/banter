"""Banter — miscellaneous reusable widgets (LoadingRow, ImageAttachment, DateSeparator)."""

from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, GdkPixbuf, Gdk

from ..constants import dbg, esc, CACHE_DIR
from ..helpers import load_image_async


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
        dialog = Adw.Dialog()
        dialog.set_title("Image")
        dialog.set_content_width(700)
        dialog.set_content_height(600)

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_vexpand(True)

        key = _cache_key(self._url)
        cached = CACHE_DIR / f"{key}.img"
        if cached.exists():
            picture.set_filename(str(cached))

        # Save button
        save_btn = Gtk.Button(label="Save Image")
        save_btn.add_css_class("suggested-action")
        save_btn.set_margin_start(12)
        save_btn.set_margin_end(12)
        save_btn.set_margin_bottom(12)
        save_btn.connect("clicked", self._save_image, dialog)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(picture)
        box.append(save_btn)

        tv.set_content(box)
        dialog.set_child(tv)
        dialog.present(self.parent_window)

    def _save_image(self, btn, dialog):
        fd = Gtk.FileDialog()
        fd.set_title("Save Image")
        fd.set_initial_name("groupme_image.jpg")
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

