"""Banter — CreateEventDialog and CreatePollDialog."""

import threading
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib

from ..constants import dbg, esc



class CreateEventDialog(Adw.Dialog):
    def __init__(self, api, group, parent):
        super().__init__()
        self._api    = api
        self._group  = group
        self._parent = parent

        self.set_title("Create Event")
        self.set_content_width(380)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(16)

        grp = Adw.PreferencesGroup(title="Event Details")
        self._name_row = Adw.EntryRow(title="Event Name *")
        grp.add(self._name_row)

        # Start time — ISO-8601 string
        self._start_row = Adw.EntryRow(title="Start (YYYY-MM-DDTHH:MM:SS)")
        self._start_row.set_text(
            datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        grp.add(self._start_row)

        self._end_row = Adw.EntryRow(title="End (optional)")
        grp.add(self._end_row)

        self._loc_row = Adw.EntryRow(title="Location (optional)")
        grp.add(self._loc_row)
        box.append(grp)

        self._btn = Gtk.Button(label="Create Event")
        self._btn.add_css_class("suggested-action")
        self._btn.add_css_class("pill")
        self._btn.connect("clicked", self._create)
        box.append(self._btn)

        tv.set_content(box)
        self.set_child(tv)

    def _create(self, *_):
        name  = self._name_row.get_text().strip()
        start = self._start_row.get_text().strip()
        end   = self._end_row.get_text().strip() or None
        loc   = self._loc_row.get_text().strip()
        if not name or not start:
            self._parent.toast("Name and start time are required"); return
        self._btn.set_sensitive(False)
        def worker():
            r = self._api.create_event(
                self._group["id"], name, start, end, loc)
            GLib.idle_add(self._done, r)
        threading.Thread(target=worker, daemon=True).start()

    def _done(self, r):
        self._btn.set_sensitive(True)
        if r:
            self._parent.toast("Event created!")
            self.close()
        else:
            self._parent.toast("Failed to create event")


# ─────────────────────────── Create Poll Dialog ──────────────────

class CreatePollDialog(Adw.Dialog):
    def __init__(self, api, group, parent):
        super().__init__()
        self._api    = api
        self._group  = group
        self._parent = parent
        self._option_rows = []

        self.set_title("Add Poll")
        self.set_content_width(400)
        self.set_content_height(560)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(16)

        q_grp = Adw.PreferencesGroup(title="Question")
        self._subject = Adw.EntryRow(title="Question *")
        q_grp.add(self._subject)
        box.append(q_grp)

        self._opts_grp = Adw.PreferencesGroup(title="Options (minimum 2)")
        box.append(self._opts_grp)
        self._add_option()
        self._add_option()

        add_opt_btn = Gtk.Button(label="+ Add Option")
        add_opt_btn.add_css_class("flat")
        add_opt_btn.connect("clicked", lambda *_: self._add_option())
        box.append(add_opt_btn)

        settings_grp = Adw.PreferencesGroup(title="Settings")
        self._multi = Adw.SwitchRow(title="Allow multiple choices")
        settings_grp.add(self._multi)

        self._expiry = Adw.SpinRow.new_with_range(1, 168, 1)
        self._expiry.set_title("Expires after (hours)")
        self._expiry.set_value(24)
        settings_grp.add(self._expiry)
        box.append(settings_grp)

        self._btn = Gtk.Button(label="Create Poll")
        self._btn.add_css_class("suggested-action")
        self._btn.add_css_class("pill")
        self._btn.connect("clicked", self._create)
        box.append(self._btn)

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    def _add_option(self):
        n   = len(self._option_rows) + 1
        row = Adw.EntryRow(title=f"Option {n}")
        self._opts_grp.add(row)
        self._option_rows.append(row)

    def _create(self, *_):
        subject = self._subject.get_text().strip()
        options = [r.get_text().strip() for r in self._option_rows
                   if r.get_text().strip()]
        if not subject:
            self._parent.toast("Question is required"); return
        if len(options) < 2:
            self._parent.toast("At least 2 options required"); return
        self._btn.set_sensitive(False)
        expiry_secs = int(self._expiry.get_value()) * 3600
        multi       = self._multi.get_active()
        def worker():
            r = self._api.create_poll(
                self._group["id"], subject, options, expiry_secs, multi)
            GLib.idle_add(self._done, r)
        threading.Thread(target=worker, daemon=True).start()

    def _done(self, r):
        self._btn.set_sensitive(True)
        if r:
            self._parent.toast("Poll created!")
            self.close()
        else:
            self._parent.toast("Failed to create poll")


# ─────────────────────────── Main Window ─────────────────────────

