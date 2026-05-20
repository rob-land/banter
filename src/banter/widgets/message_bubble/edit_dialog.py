"""EditMessageDialog — inline edit-message form bound to a MessageBubble.

Lives in its own module so `bubble.py` doesn't have to import it eagerly
(it's only constructed lazily from the right-click menu's Edit action).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from gi.repository import Adw, Gtk

from ...async_utils import run_in_background

if TYPE_CHECKING:
    from .bubble import MessageBubble


class EditMessageDialog(Adw.Dialog):
    """Inline editor for a sent message. PUTs the new text on save and
    invokes the bubble's refresh() with the server response."""

    def __init__(self, bubble: MessageBubble):
        super().__init__()
        self._bubble = bubble
        self._in_flight = False
        self.set_title("Edit message")
        self.set_content_width(420)

        tv = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        hdr.pack_start(cancel_btn)
        self._save_btn = Gtk.Button(label="Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._save)
        hdr.pack_end(self._save_btn)
        tv.add_top_bar(hdr)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)

        self._tv = Gtk.TextView()
        self._tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._tv.set_pixels_above_lines(4)
        self._tv.set_pixels_below_lines(4)
        self._tv.get_buffer().set_text(bubble.msg.get("text") or "")

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self._tv)
        scroll.set_min_content_height(120)
        scroll.set_max_content_height(360)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        body.append(scroll)
        tv.set_content(body)
        self.set_child(tv)

    def _save(self, *_):
        if self._in_flight:
            return
        buf = self._tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(),
                              buf.get_end_iter(), False).strip()
        if not text:
            return
        bubble  = self._bubble
        conv_id = bubble._conversation_id()
        mid     = str(bubble.msg.get("id"))

        # Lock the button + dialog while the request is in flight so
        # repeated clicks don't fan out into a wall of duplicate POSTs.
        self._in_flight = True
        self._save_btn.set_sensitive(False)
        self._save_btn.set_label("Saving…")

        def worker():
            return bubble.api.edit_message(conv_id, mid, text)

        def on_done(updated):
            self._in_flight = False
            if updated:
                # Stamp updated_at so the (edited) indicator shows
                # immediately — server response may be sparse.
                edited_msg = dict(bubble.msg)
                edited_msg["text"]       = text
                edited_msg["updated_at"] = int(
                    datetime.now().timestamp())
                if isinstance(updated, dict):
                    edited_msg.update({k: v for k, v in updated.items()
                                       if v is not None})
                bubble.update_text_from(edited_msg)
                try:
                    bubble.win.toast("Message updated")
                except Exception:
                    pass
                self.close()
            else:
                self._save_btn.set_label("Save")
                self._save_btn.set_sensitive(True)
                try:
                    bubble.win.toast("Failed to edit message")
                except Exception:
                    pass

        run_in_background(worker, on_done)
