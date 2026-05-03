"""Banter — CreateEventDialog, EventsListDialog, EventDetailDialog, CreatePollDialog."""

from datetime import datetime, timezone
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from ..constants import esc
from ..async_utils import run_in_background
from ..widgets.base import StandardDialog


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
    """Return a human-readable date/time string for an event dict.
    GroupMe emits start_at/end_at in UTC; convert to the user's local
    time before formatting so the displayed time matches wall-clock."""
    all_day = event.get("is_all_day", event.get("all_day", False))
    start   = _parse_dt(event.get("start_at", ""))
    end     = _parse_dt(event.get("end_at", ""))

    def _to_local(dt):
        if dt is None:
            return None
        # _parse_dt returns naive datetimes even for "...Z" strings.
        # Attach UTC then convert.
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

    if not all_day:
        start = _to_local(start)
        end   = _to_local(end)

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

class CreateEventDialog(StandardDialog):
    def __init__(self, api, group, parent):
        super().__init__(title="Create Event", width=400, height=620)
        self._api    = api
        self._group  = group
        self._parent = parent

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        self.add_header_widget(cancel_btn, end=False)

        self._btn = Gtk.Button(label="Create")
        self._btn.add_css_class("suggested-action")
        self._btn.connect("clicked", self._create)
        self.add_header_widget(self._btn, end=True)

        box = self.set_scrolled_body(margin=16, spacing=16)

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
        # A Gtk.Calendar is both more compact (doesn't squeeze the "Date"
        # label across three lines) and better on a touch screen than
        # Y/M/D spinners.
        date_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        date_lbl = Gtk.Label(label="Date", xalign=0)
        date_lbl.add_css_class("heading")
        date_section.append(date_lbl)

        self._calendar = Gtk.Calendar()
        self._calendar.add_css_class("card")
        today = GLib.DateTime.new_local(now.year, now.month, now.day, 0, 0, 0)
        self._calendar.select_day(today)
        date_section.append(self._calendar)

        box.append(date_section)

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

    def _on_all_day_toggled(self, row, _param):
        self._time_grp.set_visible(not row.get_active())

    def _on_end_toggled(self, row, _param):
        self._end_row.set_visible(row.get_active())

    def _selected_date(self):
        """Return (year, month, day) from the calendar widget."""
        gdt = self._calendar.get_date()
        return gdt.get_year(), gdt.get_month(), gdt.get_day_of_month()

    def _build_iso(self, h, m):
        """Combine the calendar-selected date with an H:M local time,
        converting to UTC ISO ending in 'Z'."""
        y, mo, d = self._selected_date()
        try:
            local = datetime(y, mo, d, h, m, 0).astimezone()
            return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None

    def _create(self, *_):
        name = self._name_row.get_text().strip()
        if not name:
            self._parent.toast("Event title is required")
            return

        all_day = self._all_day_row.get_active()

        if all_day:
            y, mo, d = self._selected_date()
            try:
                start = datetime(y, mo, d).strftime("%Y-%m-%dT00:00:00Z")
                end   = datetime(y, mo, d, 23, 59, 59).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                self._parent.toast("Invalid date")
                return
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
        run_in_background(worker)

    def _done(self, r):
        self._btn.set_sensitive(True)
        self._btn.set_label("Create")
        meta = (r or {}).get("meta", {})
        if meta.get("code") in (200, 201):
            self._parent.toast("Event created!")
            self.close()
            return
        errs = meta.get("errors") or []
        code = meta.get("code", "?")
        msg  = errs[0] if errs else f"HTTP {code}"
        self._parent.toast(f"Event failed: {msg}")


# ─────────────────────────── Events List Dialog ──────────────────

class EventsListDialog(StandardDialog):
    def __init__(self, api, group, me_id, parent):
        super().__init__(title="Events", width=420, height=560)
        self._api    = api
        self._group  = group
        self._me_id  = str(me_id)
        self._parent = parent

        # Outer stack: loading spinner vs loaded tabs
        self._outer_stack = Gtk.Stack()
        self._outer_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        spinner = Gtk.Spinner(spinning=True)
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        self._outer_stack.add_named(spinner, "loading")

        # Tab stack — Upcoming (default) / Past
        self._tab_stack = Adw.ViewStack()

        self._upcoming_list, up_wrap = self._make_tab_page("No upcoming events")
        self._past_list,     pa_wrap = self._make_tab_page("No past events")

        self._tab_stack.add_titled_with_icon(
            up_wrap, "upcoming", "Upcoming",
            "x-office-calendar-symbolic")
        self._tab_stack.add_titled_with_icon(
            pa_wrap, "past", "Past",
            "document-open-recent-symbolic")

        switcher = Adw.ViewSwitcherBar()
        switcher.set_stack(self._tab_stack)
        switcher.set_reveal(True)

        tabs_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tabs_body.append(self._tab_stack)
        tabs_body.append(switcher)
        self._outer_stack.add_named(tabs_body, "tabs")

        self.set_body(self._outer_stack)

        run_in_background(self._load)

    def _make_tab_page(self, empty_msg):
        """Return (ListBox, scroll-wrapped container) for a tab."""
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_kinetic_scrolling(True)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        list_box.set_margin_start(12); list_box.set_margin_end(12)
        list_box.set_margin_top(12);   list_box.set_margin_bottom(12)

        empty_lbl = Gtk.Label(label=empty_msg)
        empty_lbl.add_css_class("dim-label")
        empty_lbl.set_halign(Gtk.Align.CENTER)
        empty_lbl.set_valign(Gtk.Align.CENTER)
        empty_lbl.set_margin_top(48)
        empty_lbl.set_visible(False)
        list_box._empty_lbl = empty_lbl  # stash for _populate

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(list_box)
        outer.append(empty_lbl)
        scroll.set_child(outer)
        return list_box, scroll

    def _load(self):
        events = self._api.get_events(self._group["id"])
        GLib.idle_add(self._populate, events)

    def _populate(self, events):
        self._outer_stack.set_visible_child_name("tabs")
        events = events or []

        now = datetime.now()
        upcoming = []
        past     = []
        for ev in events:
            # Use end_at if present, else start_at; treat UTC, compare in local
            end_raw = ev.get("end_at") or ev.get("start_at")
            dt = _parse_dt(end_raw)
            if dt is not None:
                dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
            if dt and dt < now:
                past.append(ev)
            else:
                upcoming.append(ev)

        upcoming.sort(key=lambda e: _parse_dt(e.get("start_at", "")) or datetime.max)
        past.sort(key=lambda e: _parse_dt(e.get("start_at", "")) or datetime.min,
                  reverse=True)

        self._fill_tab(self._upcoming_list, upcoming)
        self._fill_tab(self._past_list,     past)

    def _fill_tab(self, list_box, events):
        if not events:
            list_box._empty_lbl.set_visible(True)
            return
        list_box._empty_lbl.set_visible(False)
        for ev in events:
            row = Adw.ActionRow()
            row.set_title(esc(ev.get("name", "Event")))
            row.set_activatable(True)

            subtitle = _fmt_event_time(ev)
            loc = ev.get("location", {})
            if isinstance(loc, dict):
                loc = loc.get("name", "")
            if loc:
                subtitle += "  •  " + loc
            row.set_subtitle(esc(subtitle))

            icon = Gtk.Image.new_from_icon_name("x-office-calendar-symbolic")
            icon.set_pixel_size(20)
            row.add_prefix(icon)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))

            ev_copy = dict(ev)
            row.connect("activated", lambda _r, e=ev_copy: self._open_event(e))
            list_box.append(row)

    def _open_event(self, ev):
        EventDetailDialog(self._api, self._group, ev,
                          self._me_id, self._parent).present(self._parent)


# ─────────────────────────── Event Detail Dialog ─────────────────

class EventDetailDialog(StandardDialog):
    def __init__(self, api, group, event, me_id, parent):
        super().__init__(title=esc(event.get("name", "Event")),
                         width=400, height=540)
        self._api    = api
        self._group  = group
        self._event  = event
        self._me_id  = str(me_id)
        self._parent = parent
        # GroupMe emits event ids under either `event_id` (list endpoint)
        # or `id` (detail endpoint) — accept both so RSVP always targets
        # a real event.
        self._ev_id  = event.get("event_id") or event.get("id") or ""

        box = self.set_scrolled_body(margin=16, spacing=16)

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

        self._going_btn = Gtk.Button(label="I'm in")
        self._going_btn.add_css_class("pill")
        if my_status == "going":
            self._going_btn.add_css_class("suggested-action")
        self._going_btn.connect("clicked", self._rsvp, "going")
        btn_box.append(self._going_btn)

        self._not_going_btn = Gtk.Button(label="Can't go")
        self._not_going_btn.add_css_class("pill")
        # Selected state uses suggested-action like Going — destructive
        # would imply this RSVP is dangerous, which it isn't.
        if my_status == "not_going":
            self._not_going_btn.add_css_class("suggested-action")
        self._not_going_btn.connect("clicked", self._rsvp, "not_going")
        btn_box.append(self._not_going_btn)

        box.append(btn_box)

    def _rsvp(self, _btn, status):
        self._going_btn.set_sensitive(False)
        self._not_going_btn.set_sensitive(False)

        def worker():
            ok = self._api.rsvp_event(self._group["id"], self._ev_id, status)
            GLib.idle_add(self._on_rsvp_done, ok, status)
        run_in_background(worker)

    def _on_rsvp_done(self, ok, status):
        self._going_btn.set_sensitive(True)
        self._not_going_btn.set_sensitive(True)
        if ok:
            label = "I'm in" if status == "going" else "Can't go"
            self._parent.toast(f"RSVP: {label}")
            self._going_btn.remove_css_class("suggested-action")
            self._not_going_btn.remove_css_class("suggested-action")
            if status == "going":
                self._going_btn.add_css_class("suggested-action")
            else:
                self._not_going_btn.add_css_class("suggested-action")
        else:
            self._parent.toast("RSVP failed")


# ─────────────────────────── Create Poll Dialog ──────────────────

class CreatePollDialog(StandardDialog):
    def __init__(self, api, group, parent):
        super().__init__(title="Add Poll", width=400, height=540)
        self._api    = api
        self._group  = group
        self._parent = parent
        self._option_rows = []

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        self.add_header_widget(cancel_btn, end=False)

        self._btn = Gtk.Button(label="Create")
        self._btn.add_css_class("suggested-action")
        self._btn.connect("clicked", self._create)
        self.add_header_widget(self._btn, end=True)

        box = self.set_scrolled_body(margin=16, spacing=16)

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
        run_in_background(worker)

    def _done(self, r):
        self._btn.set_sensitive(True)
        if r:
            self._parent.toast("Poll created!")
            self.close()
        else:
            self._parent.toast("Failed to create poll")
