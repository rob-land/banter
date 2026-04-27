"""Banter — EventCard: inline calendar event preview inside a message bubble."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, GLib, Gdk, Pango

from ..constants import esc
from ..async_utils import run_in_background
from ..dialogs.events import _fmt_event_time, EventDetailDialog


class EventCard(Gtk.Box):
    """Inline event card rendered inside a MessageBubble.

    Shows the event name, time, and location (if any). Provides
    "I'm in" / "Can't go" buttons that RSVP directly, and makes the
    card itself clickable to open the full EventDetailDialog.

    If the attachment gave us an embedded event dict, we render it
    immediately. Otherwise we fetch lazily via api.get_event().
    """

    def __init__(self, api, gid, me_id, event_id, event_data, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("event-card")

        self.api      = api
        self.gid      = str(gid)
        self.me       = str(me_id)
        self._event_id = str(event_id) if event_id else ""
        self._event    = event_data if isinstance(event_data, dict) else None
        self.win      = window

        # ── Header row: icon + title ──
        hdr = Gtk.Box(spacing=8)
        hdr.set_margin_bottom(2)
        icon = Gtk.Image.new_from_icon_name("x-office-calendar-symbolic")
        icon.set_pixel_size(18)
        hdr.append(icon)

        self._title_lbl = Gtk.Label(label="Loading event…")
        self._title_lbl.add_css_class("heading")
        self._title_lbl.set_halign(Gtk.Align.START)
        self._title_lbl.set_hexpand(True)
        self._title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_lbl.set_max_width_chars(30)
        hdr.append(self._title_lbl)
        self.append(hdr)

        # ── Time / location rows ──
        self._time_lbl = Gtk.Label()
        self._time_lbl.add_css_class("dim-caption")
        self._time_lbl.set_halign(Gtk.Align.START)
        self._time_lbl.set_wrap(True)
        self._time_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._time_lbl.set_max_width_chars(40)
        self.append(self._time_lbl)

        self._loc_lbl = Gtk.Label()
        self._loc_lbl.add_css_class("dim-caption")
        self._loc_lbl.set_halign(Gtk.Align.START)
        self._loc_lbl.set_wrap(True)
        self._loc_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._loc_lbl.set_max_width_chars(40)
        self._loc_lbl.set_visible(False)
        self.append(self._loc_lbl)

        # ── RSVP buttons ──
        btn_row = Gtk.Box(spacing=6, homogeneous=True)
        btn_row.set_margin_top(6)

        self._in_btn = Gtk.Button(label="I'm in")
        self._in_btn.add_css_class("pill")
        self._in_btn.connect("clicked", self._on_rsvp_click, "going")
        btn_row.append(self._in_btn)

        self._out_btn = Gtk.Button(label="Can't go")
        self._out_btn.add_css_class("pill")
        self._out_btn.connect("clicked", self._on_rsvp_click, "not_going")
        btn_row.append(self._out_btn)
        self.append(btn_row)

        # ── Clickable card (opens full event details) ──
        # Use a separate event controller on the header/time area rather
        # than the whole card, so RSVP button clicks aren't shadowed.
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        gest = Gtk.GestureClick()
        gest.connect("pressed", self._on_card_click)
        # Attach to header so the whole area above the buttons opens details
        hdr.add_controller(gest)

        time_gest = Gtk.GestureClick()
        time_gest.connect("pressed", self._on_card_click)
        self._time_lbl.add_controller(time_gest)

        if self._event:
            self._render()
        elif self._event_id:
            self._fetch()
        else:
            self._title_lbl.set_label("Event unavailable")
            self._in_btn.set_sensitive(False)
            self._out_btn.set_sensitive(False)

    # ── Fetch ──
    def _fetch(self):
        gid, eid = self.gid, self._event_id

        def worker():
            ev = self.api.get_event(gid, eid)
            GLib.idle_add(self._on_fetched, ev)
        run_in_background(worker)

    def _on_fetched(self, ev):
        if ev:
            self._event = ev
            self._render()
        else:
            self._title_lbl.set_label("Event unavailable")
            self._in_btn.set_sensitive(False)
            self._out_btn.set_sensitive(False)

    # ── Render ──
    def _render(self):
        ev = self._event or {}
        self._title_lbl.set_label(esc(ev.get("name", "Event")))
        self._time_lbl.set_label(esc(_fmt_event_time(ev)))

        loc = ev.get("location", {})
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        if loc:
            self._loc_lbl.set_label(esc("📍 " + loc))
            self._loc_lbl.set_visible(True)
        else:
            self._loc_lbl.set_visible(False)

        # Highlight current RSVP
        going     = [str(u) for u in ev.get("going", [])]
        not_going = [str(u) for u in ev.get("not_going", [])]
        self._in_btn.remove_css_class("suggested-action")
        self._out_btn.remove_css_class("destructive-action")
        if self.me in going:
            self._in_btn.add_css_class("suggested-action")
        elif self.me in not_going:
            self._out_btn.add_css_class("destructive-action")

    # ── RSVP ──
    def _on_rsvp_click(self, _btn, status):
        if not self._event_id:
            return
        self._in_btn.set_sensitive(False)
        self._out_btn.set_sensitive(False)

        gid, eid = self.gid, self._event_id
        me = self.me

        def worker():
            ok = self.api.rsvp_event(gid, eid, status)
            GLib.idle_add(self._on_rsvp_done, ok, status)
        run_in_background(worker)

    def _on_rsvp_done(self, ok, status):
        self._in_btn.set_sensitive(True)
        self._out_btn.set_sensitive(True)
        if not ok:
            self.win.toast("RSVP failed")
            return

        # Optimistically update local state + highlight
        ev = self._event or {}
        going     = [str(u) for u in ev.get("going", [])]
        not_going = [str(u) for u in ev.get("not_going", [])]
        me = self.me
        going     = [u for u in going     if u != me]
        not_going = [u for u in not_going if u != me]
        if status == "going":
            going.append(me)
        else:
            not_going.append(me)
        ev["going"]     = going
        ev["not_going"] = not_going
        self._event = ev
        self._render()

        self.win.toast("RSVP: " + ("I'm in" if status == "going" else "Can't go"))

    # ── Click card → open full details ──
    def _on_card_click(self, _gest, _n, _x, _y):
        if not self._event:
            return
        EventDetailDialog(
            self.api, {"id": self.gid}, dict(self._event),
            self.me, self.win).present(self.win)
