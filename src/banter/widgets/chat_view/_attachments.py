"""AttachmentsMixin — file picker + pending image/file preview state.

Mixed into ChatView. The picker fans out by MIME: image/* uses the
image-upload path, anything else goes through the file upload. Result
is stashed as `_pending_img_url` or `(_pending_file_id, _pending_file_name)`
which the host's `_send` includes as an attachment on the next message.
"""

import mimetypes
from pathlib import Path

from gi.repository import GLib, Gtk

from ...async_utils import run_in_background


class AttachmentsMixin:
    def _pick_attachment(self, *_):
        """Open the system file picker. Picked files are routed by MIME
        type: images via the existing image-upload path (rendered inline
        on receivers), everything else via the file-upload path
        (rendered as a download link)."""
        fd = Gtk.FileDialog()
        fd.set_title("Attach file")
        fd.open(self._win, None, self._on_attachment_picked)

    def _on_attachment_picked(self, fd, result):
        try:
            f    = fd.open_finish(result)
            path = f.get_path()
        except GLib.Error:
            return

        mime, _ = mimetypes.guess_type(path)
        is_image = bool(mime and mime.startswith("image/"))

        if is_image:
            self._win.toast("Uploading image…")

            def worker():
                url = self._api.upload_image(path)
                if url:
                    GLib.idle_add(self._set_pending_image, url,
                                   Path(path).name)
                else:
                    GLib.idle_add(lambda: self._win.toast(
                        "Image upload failed"))
            run_in_background(worker)
            return

        # Non-image: file attachment. Works for both groups and DMs;
        # the upload URL is /v1/{conv_id}/files where conv_id is the
        # group_id for groups or "<lo>+<hi>" for DMs.
        self._win.toast("Uploading file…")
        cid  = self._conv_id()
        name = Path(path).name

        def worker():
            file_id = self._api.upload_file(cid, path)
            if file_id:
                GLib.idle_add(self._set_pending_file, file_id, name)
            else:
                GLib.idle_add(lambda: self._win.toast(
                    "File upload failed"))
        run_in_background(worker)

    def _set_pending_image(self, url, name):
        self._pending_img_url   = url
        self._pending_file_id   = None
        self._pending_file_name = None
        self._preview_label.set_text(name)
        self._preview_bar.set_visible(True)
        self._win.toast("Image ready – press send")

    def _set_pending_file(self, file_id, name):
        self._pending_file_id   = file_id
        self._pending_file_name = name
        self._pending_img_url   = None
        self._preview_label.set_text(f"📎  {name}")
        self._preview_bar.set_visible(True)
        self._win.toast("File ready – press send")

    def _clear_attachment(self, *_):
        self._pending_img_url   = None
        self._pending_file_id   = None
        self._pending_file_name = None
        self._preview_bar.set_visible(False)
