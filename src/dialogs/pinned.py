"""Banter — PinnedDialog: list and manage pinned messages.

Backed by the undocumented v3 endpoints captured from web.groupme.com:
    GET  /v3/pinned/groups/{gid}/messages
    GET  /v3/pinned/direct_messages?other_user_id={uid}
    POST /v3/conversations/{cid}/messages/{mid}/{pin|unpin}
"""

from datetime import datetime
from gi.repository import Gtk, Adw, GLib

from ..constants import esc
from ..async_utils import run_in_background
from ..helpers import set_avatar_from_url
from ..widgets.base import StandardDialog


class PinnedDialog(StandardDialog):
    PREVIEW_LEN = 140

    def __init__(self, api, parent, *, group: dict = None,
                 other_user_id: str = None, other_user_name: str = ""):
        title = (f"Pinned – {group.get('name','')}" if group
                 else f"Pinned – {other_user_name or 'Direct Message'}")
        super().__init__(title=title, width=460, height=600)
        self._api          = api
        self._parent       = parent
        self._group        = group
        self._other_uid    = str(other_user_id) if other_user_id else None
        self._is_dm        = other_user_id is not None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)

        self._spinner = Gtk.Spinner(spinning=True, margin_top=40,
                                     halign=Gtk.Align.CENTER)
        outer.append(self._spinner)

        self._empty = Gtk.Label(label="No pinned messages")
        self._empty.add_css_class("dim-label")
        self._empty.set_margin_top(40)
        self._empty.set_visible(False)
        outer.append(self._empty)

        self._list_grp = Adw.PreferencesGroup()
        self._list_grp.set_visible(False)
        outer.append(self._list_grp)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_kinetic_scrolling(True)
        scroll.set_child(outer)
        self.set_body(scroll)

        self._refresh()

    # ── Fetch ──
    def _refresh(self):
        self._spinner.set_visible(True)
        self._spinner.start()
        self._empty.set_visible(False)
        self._list_grp.set_visible(False)

        is_dm    = self._is_dm
        gid      = self._group.get("id") if self._group else None
        other_id = self._other_uid

        def worker():
            if is_dm:
                msgs = self._api.get_pinned_dm(other_id)
            else:
                msgs = self._api.get_pinned_group(gid)
            GLib.idle_add(self._on_loaded, msgs or [])

        run_in_background(worker)

    def _on_loaded(self, msgs: list):
        self._spinner.stop()
        self._spinner.set_visible(False)

        # Clear any prior rows
        child = self._list_grp.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            try: self._list_grp.remove(child)
            except Exception: pass
            child = nxt

        if not msgs:
            self._empty.set_visible(True)
            return

        # Newest pin first.
        msgs_sorted = sorted(
            msgs, key=lambda m: int(m.get("pinned_at") or 0), reverse=True)
        for m in msgs_sorted:
            self._list_grp.add(self._make_row(m))
        self._list_grp.set_visible(True)

    # ── Row construction ──
    def _make_row(self, msg: dict) -> Adw.ActionRow:
        name    = msg.get("name", "")
        text    = (msg.get("text") or "").strip()
        if not text:
            atts = msg.get("attachments", [])
            if any(a.get("type") == "image" for a in atts):
                text = "📷 Image"
            elif atts:
                text = "📎 Attachment"
            else:
                text = "…"
        if len(text) > self.PREVIEW_LEN:
            text = text[:self.PREVIEW_LEN] + "…"

        pinned_at = int(msg.get("pinned_at") or 0)
        when = (datetime.fromtimestamp(pinned_at).strftime("%b %-d, %-I:%M %p")
                if pinned_at else "")
        subtitle = esc(text)
        if when:
            subtitle = f"{subtitle}\n<small>pinned {when}</small>"

        row = Adw.ActionRow(title=esc(name) or "—")
        row.set_subtitle(subtitle)
        row.set_subtitle_lines(4)

        av = Adw.Avatar(size=36, text=esc(name or "?"), show_initials=True)
        set_avatar_from_url(av, msg.get("avatar_url", ""))
        row.add_prefix(av)

        mid = str(msg.get("id"))

        jump_btn = Gtk.Button(icon_name="go-next-symbolic")
        jump_btn.add_css_class("flat")
        jump_btn.set_valign(Gtk.Align.CENTER)
        jump_btn.set_tooltip_text("Jump to message")
        jump_btn.connect("clicked", self._on_jump, mid)
        row.add_suffix(jump_btn)

        unpin_btn = Gtk.Button(icon_name="list-remove-symbolic")
        unpin_btn.add_css_class("flat")
        unpin_btn.set_valign(Gtk.Align.CENTER)
        unpin_btn.set_tooltip_text("Unpin")
        unpin_btn.connect("clicked", self._on_unpin, msg, row)
        row.add_suffix(unpin_btn)

        return row

    # ── Actions ──
    def _on_jump(self, _btn, mid: str):
        cv = getattr(self._parent, "_chat_view", None)
        if cv is None:
            return
        # Only jump if the bubble is already loaded — otherwise let the
        # user know they need to load more history. The official client
        # backfills here; we keep v1 simple.
        ok = False
        try:
            ok = cv.jump_to_message(mid)
        except Exception:
            ok = False
        if not ok:
            try:
                self._parent.toast(
                    "Message not loaded — scroll up first to load it")
            except Exception:
                pass
            return
        self.close()

    def _on_unpin(self, _btn, msg: dict, row: Adw.ActionRow):
        conv_id = self._conv_id_for(msg)
        mid     = str(msg.get("id"))
        api     = self._api
        parent  = self._parent
        list_grp = self._list_grp
        empty    = self._empty

        def worker():
            return api.unpin_message(conv_id, mid)

        def on_done(ok):
            if not ok:
                try: parent.toast("Failed to unpin (not allowed?)")
                except Exception: pass
                return
            try: list_grp.remove(row)
            except Exception: pass
            cv = getattr(parent, "_chat_view", None)
            if cv is not None:
                try: cv.mark_pinned(mid, False)
                except Exception: pass
            try: parent.toast("Unpinned")
            except Exception: pass
            # Show empty state if that was the last row.
            if list_grp.get_first_child() is None:
                list_grp.set_visible(False)
                empty.set_visible(True)

        run_in_background(worker, on_done)

    def _conv_id_for(self, msg: dict) -> str:
        """Compute the API conversation_id for unpin. Group messages
        carry `group_id`; DM messages carry `conversation_id` directly
        (in the 'a+b' form GroupMe wants)."""
        if not self._is_dm:
            return str(msg.get("group_id") or self._group.get("id"))
        cid = msg.get("conversation_id")
        if cid:
            return str(cid)
        # Fallback: build it from the participant ids the same way the
        # bubble does.
        me = str((getattr(self._parent, "_current_user", {}) or {}).get("id", ""))
        try:
            a, b = int(me), int(self._other_uid or 0)
            lo, hi = (a, b) if a < b else (b, a)
            return f"{lo}+{hi}"
        except (TypeError, ValueError):
            return f"{me}+{self._other_uid}"
