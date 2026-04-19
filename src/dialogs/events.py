"""Banter — CreateEventDialog, EventsListDialog, EventDetailDialog, CreatePollDialog."""

import threading
from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from ..constants import dbg, esc


# ─────────────────────────── Date/time helpers ───────────────────

def _parse_dt(s):
    """Parse a GroupMe ISO-8601 date string, returning a datetime or None."""
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fmt_event_time(event):
    """Return a human-readable date/time string for an event dict."""
    all_day = event.get("all_day", False)
    start   = _parse_dt(event.get("start_at", ""))
    end     = _parse_dt(event.get("end_at", ""))
    if not start:
        return event.get("start_at", "")
    if all_day:
        s = start.strftime("%B %-d, %Y")
        if end and end.date() != start.date():
            s += " – " + end.strftime("%B %-d, %Y")
        return s
    s = start.strftime("%B %-d, %Y  %-I:%M %p")
    if end:
        s += " – " + end.strftime("%-I:%M %p")
    return s


def _spin(min_val, max_val, value, width=2):
    sb = Gtk.SpinButton.new_with_range(min_val, max_val, 1)
    sb.set_value(value)
    sb.set_digits(0)
    sb.set_snap_to_ticks(True)
    sb.set_width_chars(width)
    sb.set_max_width_chars(width)
    sb.set_numeric(True)
    return sb


def _spin_sep(char):
    lbl = Gtk.Label(label=char)
    lbl.add_css_class("dim-label")
    lbl.set_valign(Gtk.Align.CENTER)
    return lbl


# ─────────────────────────── Create Event Dialog ─────────────────

class CreateEventDialog(Adw.Dialog):
    def __init__(self, api, group, parent):
        super().__init__()
        self._api    = api
        self._group  = group
        self._parent = parent

        self.set_title("Create Event")
        self.set_content_width(400)
        self.set_content_height(540)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(16)

        now = datetime.now()

        # ── Event details ──
        details_grp = Adw.PreferencesGroup(title="Event Details")

        self._name_row = Adw.EntryRow(title="Title *")
        details_grp.add(self._name_row)

        self._loc_row = Adw.EntryRow(title="Location (optional)")
        details_grp.add(self._loc_row)

        self._all_day_row = Adw.SwitchRow(title="All Day")
        self._all_day_row.connect("notify::active", self._on_all_day_toggled)
        details_grp.add(self._all_day_row)

        box.append(details_grp)

        # ── Date ──
        date_grp = Adw.PreferencesGroup(title="Date")

        self._year_spin  = _spin(2020, 2100, now.year,  width=4)
        self._month_spin = _spin(1,    12,   now.month, width=2)
        self._day_spin   = _spin(1,    31,   now.day,   width=2)

        date_row = Adw.ActionRow(title="Date")
        date_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        date_box.append(self._year_spin)
        date_box.append(_spin_sep("-"))
        date_box.append(self._month_spin)
        date_box.append(_spin_sep("-"))
        date_box.append(self._day_spin)
        date_row.add_suffix(date_box)
        date_grp.add(date_row)

        box.append(date_grp)

        # ── Time ──
        self._time_grp = Adw.PreferencesGroup(title="Time")

        self._start_h = _spin(0, 23, now.hour)
        self._start_m = _spin(0, 59, 0)

        start_row = Adw.ActionRow(title="Start")
        start_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        start_box.append(self._start_h)
        start_box.append(_spin_sep(":"))
        start_box.append(self._start_m)
        start_row.add_suffix(start_box)
        self._time_grp.add(start_row)

        self._has_end_row = Adw.SwitchRow(title="End Time")
        self._has_end_row.connect("notify::active", self._on_end_toggled)
        self._time_grp.add(self._has_end_row)

        self._end_h = _spin(0, 23, now.hour + 1 if now.hour < 23 else 23)
        self._end_m = _spin(0, 59, 0)

        self._end_row = Adw.ActionRow(title="End")
        end_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        end_box.append(self._end_h)
        end_box.append(_spin_sep(":"))
        end_box.append(self._end_m)
        self._end_row.add_suffix(end_box)
        self._end_row.set_visible(False)
        self._time_grp.add(self._end_row)

        box.append(self._time_grp)

        # ── Button ──
        self._btn = Gtk.Button(label="Create Event")
        self._btn.add_css_class("suggested-action")
        self._btn.add_css_class("pill")
        self._btn.connect("clicked", self._create)
        box.append(self._btn)

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    def _on_all_day_toggled(self, row, _param):
        self._time_grp.set_visible(not row.get_active())

    def _on_end_toggled(self, row, _param):
        self._end_row.set_visible(row.get_active())

    def _build_iso(self, h, m):
        y  = int(self._year_spin.get_value())
        mo = int(self._month_spin.get_value())
        d  = int(self._day_spin.get_value())
        try:
            return datetime(y, mo, d, h, m, 0).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    def _create(self, *_):
        name = self._name_row.get_text().strip()
        if not name:
            self._parent.toast("Event title is required")
            return

        all_day = self._all_day_row.get_active()

        if all_day:
            y  = int(self._year_spin.get_value())
            mo = int(self._month_spin.get_value())
            d  = int(self._day_spin.get_value())
            try:
                start = datetime(y, mo, d).strftime("%Y-%m-%dT00:00:00")
            except ValueError:
                self._parent.toast("Invalid date")
                return
            end = None
        else:
            start = self._build_iso(int(self._start_h.get_value()),
                                    int(self._start_m.get_value()))
            if not start:
                self._parent.toast("Invalid date")
                return
            end = None
            if self._has_end_row.get_active():
                end = self._build_iso(int(self._end_h.get_value()),
                                      int(self._end_m.get_value()))

        loc = self._loc_row.get_text().strip()

        self._btn.set_sensitive(False)
        self._btn.set_label("Creating…")

        def worker():
            r = self._api.create_event(
                self._group["id"], name, start, end, loc, all_day)
            GLib.idle_add(self._done, r)
        threading.Thread(target=worker, daemon=True).start()

    def _done(self, r):
        self._btn.set_sensitive(True)
        self._btn.set_label("Create Event")
        if r:
            self._parent.toast("Event created!")
            self.close()
        else:
            self._parent.toast("Failed to create event")


# ─────────────────────────── Events List Dialog ──────────────────

class EventsListDialog(Adw.Dialog):
    def __init__(self, api, group, me_id, parent):
        super().__init__()
        self._api    = api
        self._group  = group
        self._me_id  = str(me_id)
        self._parent = parent

        self.set_title("Events")
        self.set_content_width(420)
        self.set_content_height(560)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        spinner = Gtk.Spinner(spinning=True)
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        self._stack.add_named(spinner, "loading")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)

        self._empty_lbl = Gtk.Label(label="No upcoming events")
        self._empty_lbl.add_css_class("dim-label")
        self._empty_lbl.set_halign(Gtk.Align.CENTER)
        self._empty_lbl.set_valign(Gtk.Align.CENTER)
        self._empty_lbl.set_margin_top(48)
        self._empty_lbl.set_visible(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self._list_box)
        outer.append(self._empty_lbl)

        scroll.set_child(outer)
        self._stack.add_named(scroll, "list")

        tv.set_content(self._stack)
        self.set_child(tv)

        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        events = self._api.get_events(self._group["id"])
        GLib.idle_add(self._populate, events)

    def _populate(self, events):
        self._stack.set_visible_child_name("list")
        if not events:
            self._empty_lbl.set_visible(True)
            return

        for ev in events:
            row = Adw.ActionRow()
            row.set_title(esc(ev.get("name", "Event")))
            row.set_subtitle(esc(_fmt_event_time(ev)))
            row.set_activatable(True)

            loc = ev.get("location", {})
            if isinstance(loc, dict):
                loc = loc.get("name", "")
            if loc:
                row.set_subtitle(esc(_fmt_event_time(ev) + "  •  " + loc))

            icon = Gtk.Image.new_from_icon_name("x-office-calendar-symbolic")
            icon.set_pixel_size(20)
            row.add_prefix(icon)
            row.add_suffix(
                Gtk.Image.new_from_icon_name("go-next-symbolic"))

            ev_copy = dict(ev)
            row.connect("activated", lambda _r, e=ev_copy: self._open_event(e))
            self._list_box.append(row)

    def _open_event(self, ev):
        EventDetailDialog(self._api, self._group, ev,
                          self._me_id, self._parent).present(self._parent)


# ─────────────────────────── Event Detail Dialog ─────────────────

class EventDetailDialog(Adw.Dialog):
    def __init__(self, api, group, event, me_id, parent):
        super().__init__()
        self._api    = api
        self._group  = group
        self._event  = event
        self._me_id  = str(me_id)
        self._parent = parent
        self._ev_id  = event.get("id", "")

        self.set_title(esc(event.get("name", "Event")))
        self.set_content_width(400)
        self.set_content_height(540)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(16)

        # ── Title ──
        title_lbl = Gtk.Label(label=esc(event.get("name", "Event")))
        title_lbl.add_css_class("title-2")
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.set_wrap(True)
        box.append(title_lbl)

        # ── Details group ──
        details_grp = Adw.PreferencesGroup()

        time_row = Adw.ActionRow(title="When")
        time_row.set_subtitle(esc(_fmt_event_time(event)))
        time_row.add_prefix(
            Gtk.Image.new_from_icon_name("x-office-calendar-symbolic"))
        details_grp.add(time_row)

        loc = event.get("location", {})
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        if loc:
            loc_row = Adw.ActionRow(title="Where")
            loc_row.set_subtitle(esc(loc))
            loc_row.add_prefix(
                Gtk.Image.new_from_icon_name("find-location-symbolic"))
            details_grp.add(loc_row)

        desc = event.get("description", "")
        if desc:
            desc_row = Adw.ActionRow(title="Description")
            desc_row.set_subtitle(esc(desc))
            details_grp.add(desc_row)

        box.append(details_grp)

        # ── RSVP ──
        rsvp_grp = Adw.PreferencesGroup(title="RSVP")

        going_ids     = [str(u) for u in event.get("going",     [])]
        not_going_ids = [str(u) for u in event.get("not_going", [])]
        going_count   = len(going_ids)
        not_going_count = len(not_going_ids)

        going_row = Adw.ActionRow(title="Going")
        going_row.set_subtitle(str(going_count) if going_count else "No responses yet")
        going_row.add_prefix(
            Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
        rsvp_grp.add(going_row)

        not_going_row = Adw.ActionRow(title="Not Going")
        not_going_row.set_subtitle(str(not_going_count) if not_going_count else "—")
        not_going_row.add_prefix(
            Gtk.Image.new_from_icon_name("window-close-symbolic"))
        rsvp_grp.add(not_going_row)

        box.append(rsvp_grp)

        # ── RSVP buttons ──
        my_status = None
        if self._me_id in going_ids:
            my_status = "going"
        elif self._me_id in not_going_ids:
            my_status = "not_going"

        btn_box = Gtk.Box(spacing=8, homogeneous=True)

        self._going_btn = Gtk.Button(label="Going")
        self._going_btn.add_css_class("pill")
        if my_status == "going":
            self._going_btn.add_css_class("suggested-action")
        self._going_btn.connect("clicked", self._rsvp, "going")
        btn_box.append(self._going_btn)

        self._not_going_btn = Gtk.Button(label="Not Going")
        self._not_going_btn.add_css_class("pill")
        if my_status == "not_going":
            self._not_going_btn.add_css_class("destructive-action")
        self._not_going_btn.connect("clicked", self._rsvp, "not_going")
        btn_box.append(self._not_going_btn)

        box.append(btn_box)

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    def _rsvp(self, _btn, status):
        self._going_btn.set_sensitive(False)
        self._not_going_btn.set_sensitive(False)

        def worker():
            ok = self._api.rsvp_event(self._group["id"], self._ev_id, status)
            GLib.idle_add(self._on_rsvp_done, ok, status)
        threading.Thread(target=worker, daemon=True).start()

    def _on_rsvp_done(self, ok, status):
        self._going_btn.set_sensitive(True)
        self._not_going_btn.set_sensitive(True)
        if ok:
            label = "Going" if status == "going" else "Not Going"
            self._parent.toast(f"RSVP: {label}")
            # Update button styles to reflect new status
            self._going_btn.remove_css_class("suggested-action")
            self._not_going_btn.remove_css_class("destructive-action")
            if status == "going":
                self._going_btn.add_css_class("suggested-action")
            else:
                self._not_going_btn.add_css_class("destructive-action")
        else:
            self._parent.toast("RSVP failed")


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
