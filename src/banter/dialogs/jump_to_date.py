"""Banter — JumpToDateDialog: pick a calendar date to scroll the active
conversation back to."""

from datetime import date as _date, datetime
from gi.repository import Gtk, Adw


@Gtk.Template(resource_path="/land/rob/banter/ui/jump-to-date-dialog.ui")
class JumpToDateDialog(Adw.Dialog):
    __gtype_name__ = "JumpToDateDialog"

    calendar:      Gtk.Calendar = Gtk.Template.Child()
    cancel_button: Gtk.Button   = Gtk.Template.Child()
    jump_button:   Gtk.Button   = Gtk.Template.Child()

    def __init__(self, parent_window):
        super().__init__()
        self._parent = parent_window

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, _btn):
        self.close()

    @Gtk.Template.Callback()
    def on_jump_clicked(self, _btn):
        gd = self.calendar.get_date()  # GLib.DateTime
        target = _date(gd.get_year(), gd.get_month(), gd.get_day_of_month())
        # Clamp future picks to today — paging back from the future is
        # nonsensical and would spin until the batch limit hits.
        today = datetime.now().date()
        if target > today:
            target = today
        cv = getattr(self._parent, "_chat_view", None)
        if cv is not None:
            cv.jump_to_date(target)
        self.close()
