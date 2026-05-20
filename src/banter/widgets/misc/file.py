"""FileAttachment — pill-style download button for `{type:file}` attachments."""

from gi.repository import GLib, Gtk

from ...async_utils import run_in_background


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
        try:
            self._parent.toast(f"Saving {name}…")
        except Exception:
            pass

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
                except Exception:
                    pass
            GLib.idle_add(report)

        run_in_background(worker)
