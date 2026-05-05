"""Banter — PollCard: inline poll preview inside a message bubble."""

import time
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, GLib, Gdk, Pango

from ..constants import esc
from ..async_utils import run_in_background


def _fmt_status(poll: dict) -> str:
    status = (poll.get("status") or "").lower()
    if status == "ended" or status == "deleted":
        return "Poll closed"
    exp = poll.get("expiration")
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        exp = 0
    if exp <= 0:
        return ""
    remaining = exp - int(time.time())
    if remaining <= 0:
        return "Poll closed"
    if remaining < 3600:
        return f"Closes in {max(1, remaining // 60)} min"
    if remaining < 86400:
        return f"Closes in {remaining // 3600} h"
    return f"Closes in {remaining // 86400} d"


class PollCard(Gtk.Box):
    """Inline poll card rendered inside a MessageBubble.

    The "created poll" message carries a `{type: poll, poll_id}`
    attachment but no embedded data — we lazily fetch via
    `api.get_poll(gid, poll_id)`. After voting we re-fetch to pick up
    the new totals; live push updates are out of scope for the inline
    card (a chat reload picks them up, and the next vote refreshes).

    GroupMe's anonymous polls don't expose per-voter ids, so we track
    "did I vote" in local widget state — fine since the user's own
    vote is the only one we'd want to highlight anyway.
    """

    def __init__(self, api, gid, me_id, poll_id, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("poll-card")

        self.api      = api
        self.gid      = str(gid)
        self.me       = str(me_id)
        self._poll_id = str(poll_id) if poll_id else ""
        self.win      = window

        self._poll          = None
        self._option_btns   : dict = {}   # option_id -> Gtk.Button
        self._option_count  : dict = {}   # option_id -> Gtk.Label (vote count)
        self._option_bars   : dict = {}   # option_id -> Gtk.LevelBar
        self._voted_ids     : set  = set()  # option ids the user has cast
        self._busy          = False

        # ── Header row: icon + subject ──
        hdr = Gtk.Box(spacing=8)
        icon = Gtk.Image.new_from_icon_name("view-list-bullet-symbolic")
        icon.set_pixel_size(18)
        hdr.append(icon)

        self._title_lbl = Gtk.Label(label="Loading poll…")
        self._title_lbl.add_css_class("heading")
        self._title_lbl.set_halign(Gtk.Align.START)
        self._title_lbl.set_hexpand(True)
        self._title_lbl.set_wrap(True)
        self._title_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._title_lbl.set_max_width_chars(34)
        hdr.append(self._title_lbl)
        self.append(hdr)

        # ── Options container ──
        self._opts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                  spacing=4)
        self.append(self._opts_box)

        # ── Footer status line ──
        self._status_lbl = Gtk.Label()
        self._status_lbl.add_css_class("dim-caption")
        self._status_lbl.set_halign(Gtk.Align.START)
        self._status_lbl.set_visible(False)
        self.append(self._status_lbl)

        if self._poll_id:
            self._fetch()
            # Register so push poll.vote events route here.
            reg = getattr(window, "register_poll_card", None)
            if reg:
                reg(self._poll_id, self)
        else:
            self._title_lbl.set_label("Poll unavailable")

    # ── Fetch ──────────────────────────────────────────────────────────
    def _fetch(self):
        gid, pid = self.gid, self._poll_id

        def worker():
            poll = self.api.get_poll(gid, pid)
            GLib.idle_add(self._on_fetched, poll)
        run_in_background(worker)

    def _on_fetched(self, poll):
        if not poll:
            self._title_lbl.set_label("Poll unavailable")
            return
        self._poll = poll
        self._render()

    # ── Render ─────────────────────────────────────────────────────────
    def _render(self):
        p = self._poll or {}
        self._title_lbl.set_label(esc(p.get("subject", "Poll")))

        # Rebuild option rows from scratch (cheap; usually 2-5 options).
        child = self._opts_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._opts_box.remove(child)
            child = nxt
        self._option_btns.clear()
        self._option_count.clear()
        self._option_bars.clear()

        options = list(p.get("options", []))
        total = sum(int(o.get("votes") or 0) for o in options)
        closed = self._is_closed(p)

        for opt in options:
            oid    = str(opt.get("id", ""))
            title  = opt.get("title", "")
            votes  = int(opt.get("votes") or 0)
            self._opts_box.append(
                self._build_option_row(oid, title, votes, total, closed))

        status_text = _fmt_status(p)
        if status_text:
            self._status_lbl.set_label(esc(status_text))
            self._status_lbl.set_visible(True)
        else:
            self._status_lbl.set_visible(False)

    def _build_option_row(self, oid, title, votes, total, closed):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        btn = Gtk.Button()
        btn.add_css_class("poll-option")
        btn.set_sensitive(not closed and not self._busy)
        if oid in self._voted_ids:
            btn.add_css_class("suggested-action")

        inner = Gtk.Box(spacing=8)
        inner.set_margin_start(8)
        inner.set_margin_end(8)
        inner.set_margin_top(4)
        inner.set_margin_bottom(4)

        title_lbl = Gtk.Label(label=esc(title))
        title_lbl.set_use_markup(True)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.set_hexpand(True)
        title_lbl.set_xalign(0)
        title_lbl.set_wrap(True)
        title_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_lbl.set_max_width_chars(28)
        inner.append(title_lbl)

        count_lbl = Gtk.Label(label=str(votes))
        count_lbl.add_css_class("dim-caption")
        count_lbl.set_halign(Gtk.Align.END)
        inner.append(count_lbl)

        btn.set_child(inner)
        btn.connect("clicked", self._on_option_click, oid)
        row.append(btn)

        # Progress bar showing share of votes.
        bar = Gtk.LevelBar()
        bar.set_min_value(0.0)
        bar.set_max_value(1.0)
        bar.set_value((votes / total) if total > 0 else 0.0)
        bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        bar.add_css_class("poll-bar")
        row.append(bar)

        self._option_btns[oid]   = btn
        self._option_count[oid]  = count_lbl
        self._option_bars[oid]   = bar
        return row

    @staticmethod
    def _is_closed(p: dict) -> bool:
        status = (p.get("status") or "").lower()
        if status in ("ended", "deleted"):
            return True
        try:
            exp = int(p.get("expiration") or 0)
        except (TypeError, ValueError):
            exp = 0
        return exp > 0 and exp <= int(time.time())

    @staticmethod
    def _is_multi(p: dict) -> bool:
        # Wire format uses "single"/"multi"; the create endpoint sends
        # "single_choice"/"multi_choice". Accept either.
        return (p.get("type") or "").lower().startswith("multi")

    # ── Vote ───────────────────────────────────────────────────────────
    def _on_option_click(self, _btn, oid):
        if self._busy or not self._poll:
            return
        if self._is_closed(self._poll):
            return

        if self._is_multi(self._poll):
            if oid in self._voted_ids:
                # Toggle off — but GroupMe doesn't have an unvote API
                # for multi, so on click of an already-voted option we
                # cast the remaining selection (server overwrites).
                new_selection = list(self._voted_ids - {oid})
            else:
                new_selection = list(self._voted_ids | {oid})
        else:
            new_selection = [oid]

        self._busy = True
        for b in self._option_btns.values():
            b.set_sensitive(False)

        gid, pid = self.gid, self._poll_id

        def worker():
            ok = self.api.vote_poll(gid, pid, new_selection)
            poll = self.api.get_poll(gid, pid) if ok else None
            GLib.idle_add(self._on_vote_done, ok, poll, new_selection)
        run_in_background(worker)

    def _on_vote_done(self, ok, poll, selection):
        self._busy = False
        if not ok:
            self.win.toast("Vote failed")
            for b in self._option_btns.values():
                b.set_sensitive(True)
            return
        self._voted_ids = set(selection)
        if poll:
            self._poll = poll
        self._render()

    # ── Live push update ──────────────────────────────────────────────
    def apply_push_update(self, poll_data: dict):
        """Called by BanterWindow when a poll.vote push event lands for
        our poll_id. The push payload is the full poll snapshot — same
        shape as get_poll's response — so we replace _poll and re-
        render. _voted_ids is preserved (push doesn't carry per-voter
        info for anonymous polls)."""
        if not isinstance(poll_data, dict) or not poll_data:
            return
        self._poll = poll_data
        self._render()
