"""Banter — JumpToDateDialog: pick a calendar date to scroll the active
conversation back to."""

from datetime import date as _date, datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from ..widgets.base import StandardDialog


class JumpToDateDialog(StandardDialog):
    def __init__(self, parent_window):
        super().__init__(title="Jump to date", width=340, height=-1)
        self._parent = parent_window

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)

        self._cal = Gtk.Calendar()
        body.append(self._cal)

        btn_box = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        btn_box.append(cancel)
        jump = Gtk.Button(label="Jump")
        jump.add_css_class("suggested-action")
        jump.connect("clicked", self._on_jump)
        btn_box.append(jump)
        body.append(btn_box)

        self.set_body(body)

    def _on_jump(self, *_):
        gd = self._cal.get_date()  # GLib.DateTime
        target = _date(gd.get_year(),
                       gd.get_month(),
                       gd.get_day_of_month())
        # Clamp future picks to today — paging back from the future is
        # nonsensical and would spin until the batch limit hits.
        today = datetime.now().date()
        if target > today:
            target = today
        cv = getattr(self._parent, "_chat_view", None)
        if cv is not None:
            cv.jump_to_date(target)
        self.close()
