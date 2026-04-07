"""Banter — MessageBubble widget with inline reactions."""

import re
import threading
from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gdk

from ..constants import dbg, esc, DEFAULT_REACTIONS, EMOJI_LOG
from ..api import GroupMeAPI
from ..helpers import load_image_async, set_avatar_from_url
from .misc import ImageAttachment

# ── URL / email linkification ─────────────────────────────────────────
_URL_RE = re.compile(
    r"(https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+|[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)


def _linkify(text: str) -> tuple:
    """Return (pango_markup, has_links).
    Wraps URLs and email addresses in <a href="..."> tags."""
    parts = _URL_RE.split(text)
    if len(parts) == 1:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), False)

    out = []
    for i, part in enumerate(parts):
        safe = part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if i % 2 == 0:
            out.append(safe)
        else:
            if part.lower().startswith("www."):
                href = "http://" + part
            elif "@" in part and not part.lower().startswith("http"):
                href = "mailto:" + part
            else:
                href = part
            href_safe = href.replace("&", "&amp;").replace('"', "&quot;")
            out.append(f'<a href="{href_safe}">{safe}</a>')
    return ("".join(out), True)



class MessageBubble(Gtk.Box):
    def __init__(self, msg: dict, me_id, group_id: str,
                 api: GroupMeAPI, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._msg  = msg
        self._gid  = group_id
        self._api  = api
        self._win  = window
        self._me   = str(me_id)
        is_mine    = str(msg.get("user_id", "")) == self._me

        # Cache this sender's name so reaction tooltips can resolve it
        uid  = str(msg.get("user_id", ""))
        name = msg.get("name", "")
        if uid and name and hasattr(window, "cache_sender_name"):
            window.cache_sender_name(uid, name)

        # ── Sender header (others only) ──
        if not is_mine:
            hdr = Gtk.Box(spacing=6)
            hdr.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            av  = Adw.Avatar(size=28,
                             text=esc(msg.get("name", "?")),
                             show_initials=True)
            set_avatar_from_url(av, msg.get("avatar_url"))
            hdr.append(av)

            nm = Gtk.Label(label=esc(msg.get("name", "Unknown")))
            nm.add_css_class("dim-caption")
            nm.add_css_class("bold-name")
            nm.set_halign(Gtk.Align.START)
            hdr.append(nm)

            ts  = msg.get("created_at", 0)
            dt  = datetime.fromtimestamp(ts)
            tl  = Gtk.Label(label=dt.strftime("%-I:%M %p"))
            tl.add_css_class("dim-caption")
            tl.add_css_class("dim-label")
            tl.set_margin_start(4)
            hdr.append(tl)

            hdr.set_margin_start(4)

            # Click sender name/avatar → open DM
            sender_uid  = str(msg.get("user_id", ""))
            sender_info = {
                "name"      : msg.get("name", ""),
                "avatar_url": msg.get("avatar_url", ""),
                "user_id"   : sender_uid,
            }
            gest = Gtk.GestureClick()
            gest.connect("pressed",
                         lambda *_, u=sender_uid, s=sender_info:
                             window.open_dm_for_user(s, u))
            hdr.add_controller(gest)
            self.append(hdr)

        # ── Bubble ──
        # GTK4 wrapping fix: labels propagate their natural (unwrapped) width
        # upward, expanding the window. The correct pattern is:
        #   1. A row_box fills the full width (hexpand=True, no shrink)
        #   2. An expanding spacer pushes the bubble to the correct side
        #   3. The bubble itself does NOT hexpand — it sizes to content
        #   4. lbl.set_width_request(1) breaks the natural-size feedback loop
        #      so GTK stops asking the label how wide it wants to be and
        #      instead wraps it to whatever width the bubble is allocated.
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row_box.set_hexpand(True)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        # 32 px minimum gap so bubble never runs edge-to-edge
        spacer.set_size_request(32, -1)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bubble.add_css_class("msg-bubble")
        bubble.add_css_class("mine" if is_mine else "theirs")
        # bubble does NOT hexpand — it must be constrained by the spacer

        # ── Reply quote block (async) ──────────────────────────────────
        # Detect a reply attachment and reserve a placeholder that fills in
        # once the parent message is fetched from the API.
        reply_att = next(
            (a for a in msg.get("attachments", []) if a.get("type") == "reply"),
            None,
        )
        if reply_att:
            parent_id = str(reply_att.get("reply_id") or
                            reply_att.get("base_reply_id") or "")
            if parent_id:
                # Placeholder shown while loading
                self._quote_box = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL, spacing=2)
                self._quote_box.add_css_class("reply-quote")
                placeholder = Gtk.Label(label="↩  loading…")
                placeholder.add_css_class("reply-quote-text")
                placeholder.set_xalign(0)
                self._quote_box.append(placeholder)
                bubble.append(self._quote_box)

                # Fetch the parent message asynchronously
                gid = group_id
                def _fetch_parent(pid=parent_id, gid=gid):
                    msgs = api.get_messages(gid,
                                            before_id=str(int(pid) + 1),
                                            limit=1)
                    parent = next(
                        (m for m in msgs if str(m.get("id")) == pid), None)
                    GLib.idle_add(self._set_quote, parent)
                threading.Thread(target=_fetch_parent, daemon=True).start()

        text = (msg.get("text") or "").strip()
        if text:
            markup, has_links = _linkify(text)
            lbl = Gtk.Label(wrap=True)
            lbl.set_xalign(0)
            lbl.set_selectable(True)
            lbl.set_max_width_chars(45)
            if has_links:
                # Use markup so <a href> links are rendered as clickable
                lbl.set_markup(markup)
                lbl.set_use_markup(True)
                # GTK label handles link activation natively when use_markup=True
            else:
                lbl.set_text(text)
            bubble.append(lbl)

        for att in msg.get("attachments", []):
            kind = att.get("type")
            if kind == "reply":
                continue   # handled as the quote block above
            if kind == "image":
                img = ImageAttachment(att["url"], window)
                bubble.append(img)
            elif kind == "location":
                loc = Gtk.Label(
                    label=f"📍 {att.get('name','Location')}"
                          f"\n{att.get('lat')}, {att.get('lng')}")
                loc.set_wrap(True)
                loc.set_xalign(0)
                bubble.append(loc)
            elif kind == "split":
                lbl = Gtk.Label(label="💳 Split request")
                bubble.append(lbl)
            elif kind == "emoji":
                pass  # GroupMe emoji packs – skip for now

        # Timestamp (mine only, shown inside bubble)
        if is_mine:
            ts = msg.get("created_at", 0)
            tl = Gtk.Label(label=datetime.fromtimestamp(ts).strftime("%-I:%M %p"))
            tl.add_css_class("dim-caption")
            tl.set_halign(Gtk.Align.END)
            bubble.append(tl)

        if is_mine:
            row_box.append(spacer)
            row_box.append(bubble)
        else:
            row_box.append(bubble)
            row_box.append(spacer)
        self.append(row_box)

        # ── Reactions row ──
        self._reactions_box = Gtk.Box(spacing=4)
        self._reactions_box.set_halign(
            Gtk.Align.END if is_mine else Gtk.Align.START)
        self._reactions_box.set_margin_start(8)
        self._reactions_box.set_margin_end(8)
        self._reactions_box.set_margin_top(2)
        self.append(self._reactions_box)

        self.set_margin_bottom(6)
        self.set_margin_start(8)
        self.set_margin_end(8)

        # Render initial state
        self._render_reactions(msg.get("reactions", []),
                               msg.get("favorited_by", []))

    # ── Reply quote ──
    def _set_quote(self, parent_msg):
        """Fill in the reply quote block once the parent message is loaded."""
        if not hasattr(self, "_quote_box"):
            return
        # Clear the placeholder
        child = self._quote_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._quote_box.remove(child)
            child = nxt

        if not parent_msg:
            lbl = Gtk.Label(label="↩  Original message unavailable")
            lbl.add_css_class("reply-quote-text")
            lbl.set_xalign(0)
            self._quote_box.append(lbl)
            return

        # Sender name
        name_lbl = Gtk.Label(label=esc(parent_msg.get("name", "Unknown")))
        name_lbl.add_css_class("reply-quote-name")
        name_lbl.set_xalign(0)
        self._quote_box.append(name_lbl)

        # Message preview (first 120 chars)
        parent_text = (parent_msg.get("text") or "").strip()
        if not parent_text:
            # Image or attachment with no text
            atts = parent_msg.get("attachments", [])
            if any(a.get("type") == "image" for a in atts):
                parent_text = "📷 Image"
            elif atts:
                parent_text = "📎 Attachment"
            else:
                parent_text = "…"

        MAX_LEN = 120
        preview = parent_text[:MAX_LEN] + ("…" if len(parent_text) > MAX_LEN else "")
        text_lbl = Gtk.Label(label=preview)
        text_lbl.add_css_class("reply-quote-text")
        text_lbl.set_xalign(0)
        text_lbl.set_wrap(True)
        text_lbl.set_max_width_chars(40)
        self._quote_box.append(text_lbl)

    # ── Reactions ──
    def _render_reactions(self, reactions: list, favorited_by: list):
        """Rebuild the reactions row from server data."""
        # Clear
        child = self._reactions_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._reactions_box.remove(child)
            child = nxt

        # Build reaction_map: display_code → {uids, raw_code, is_emoji_pack}
        # emoji-pack reactions (type="emoji") are shown as 😊 and logged
        reaction_map = {}   # display_code → {"uids": set, "raw_code": str, "is_pack": bool}
        for r in (reactions or []):
            r_type = r.get("type", "unicode")
            if r_type == "emoji":
                display = "😊"
                is_pack = True
                # Log the raw emoji reaction data
                try:
                    with open(EMOJI_LOG, "a") as f:
                        f.write(f"{datetime.now().isoformat()}  msg={self._msg.get('id')}  {r}\n")
                except Exception:
                    pass
            else:
                display = r.get("code") or "❤️"
                is_pack = False
            uid_list = r.get("user_ids", [])
            if display not in reaction_map:
                reaction_map[display] = {"uids": set(), "raw_code": display, "is_pack": is_pack}
            reaction_map[display]["uids"].update(str(u) for u in uid_list)

        # Legacy heart likes fallback
        if favorited_by and not reaction_map:
            uids = set(str(u) for u in favorited_by)
            reaction_map["❤️"] = {"uids": uids, "raw_code": "❤️", "is_pack": False}

        me          = self._me
        i_reacted   = any(me in v["uids"] for v in reaction_map.values())
        my_code     = next((c for c, v in reaction_map.items() if me in v["uids"]), None)

        # Build member-name lookup from the group/DM cache if available
        def _uid_names(uid_set: set) -> str:
            """Return comma-joined display names for a set of user IDs."""
            names = []
            for uid in uid_set:
                if uid == me:
                    names.append("You")
                else:
                    # Try to look up from the window's member cache
                    name = self._win.get_user_name(uid) if hasattr(self._win, "get_user_name") else uid
                    names.append(name)
            return ", ".join(sorted(names)) if names else "?"

        # Reaction pills
        for code, info in reaction_map.items():
            uid_set = info["uids"]
            count   = len(uid_set)
            i_used  = me in uid_set

            pill = Gtk.Button()
            pill.add_css_class("flat")
            pill.add_css_class("reaction-pill")
            if i_used:
                pill.add_css_class("reaction-pill-mine")

            lbl = Gtk.Label(label=f"{code} {count}")
            lbl.set_use_markup(False)
            pill.set_child(lbl)

            # Tooltip: list of reactors
            pill.set_tooltip_text(_uid_names(uid_set))

            raw_code = info["raw_code"]
            pill.connect("clicked", self._on_reaction_pill, raw_code, i_used, my_code)
            self._reactions_box.append(pill)

        # "+" button — only when the user has NOT reacted yet
        if not i_reacted:
            add_btn = Gtk.Button()
            add_btn.set_icon_name("list-add-symbolic")
            add_btn.add_css_class("flat")
            add_btn.add_css_class("circular")
            add_btn.set_tooltip_text("Add reaction")
            # Store known reaction codes from this message for the picker
            existing = list(reaction_map.keys())
            add_btn.connect("clicked", self._on_add_reaction, existing)
            self._reactions_box.append(add_btn)

        # Click on the reactions box itself (if there are reactions) → detail dialog
        if reaction_map:
            gest = Gtk.GestureClick()
            gest.connect("pressed",
                         lambda *_: self._show_reaction_detail(reaction_map))
            self._reactions_box.add_controller(gest)

    def _on_reaction_pill(self, btn, code: str, i_used: bool, my_code: str | None):
        """Handle a reaction pill click.

        - Own reaction  → remove it
        - Different user's reaction, no current reaction → add that reaction
        - Different user's reaction, already reacted differently → change reaction
        """
        gid = self._gid
        mid = self._msg["id"]
        if i_used:
            # Remove own reaction
            def worker():
                ok = self._api.unreact_message(gid, mid)
                if ok:
                    GLib.idle_add(self._refresh_from_server)
            threading.Thread(target=worker, daemon=True).start()
        else:
            # Add/change to this reaction code
            def worker():
                # If we have a different reaction, remove it first
                if my_code:
                    self._api.unreact_message(gid, mid)
                ok = self._api.react_message(gid, mid, code)
                if ok:
                    GLib.idle_add(self._refresh_from_server)
            threading.Thread(target=worker, daemon=True).start()

    def _on_add_reaction(self, btn, existing_codes: list):
        """Show the reaction picker popover."""
        # Merge default reactions with any seen on this message
        seen = [c for c in existing_codes if c != "😊"]   # exclude emoji-pack placeholder
        offered = list(dict.fromkeys(DEFAULT_REACTIONS + seen))   # deduplicated, order preserved

        popover = Gtk.Popover()
        popover.set_parent(btn)

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(5)
        flow.set_max_children_per_line(10)
        flow.set_column_spacing(2)
        flow.set_row_spacing(2)
        flow.set_margin_start(6); flow.set_margin_end(6)
        flow.set_margin_top(6);  flow.set_margin_bottom(6)

        for emoji in offered:
            eb = Gtk.Button(label=emoji)
            eb.add_css_class("flat")
            eb.set_size_request(36, 36)
            code = emoji
            def on_pick(b, c=code):
                popover.popdown()
                gid = self._gid
                mid = self._msg["id"]
                def worker():
                    ok = self._api.react_message(gid, mid, c)
                    if ok:
                        GLib.idle_add(self._refresh_from_server)
                threading.Thread(target=worker, daemon=True).start()
            eb.connect("clicked", on_pick)
            flow.append(eb)

        popover.set_child(flow)
        popover.popup()

    def _show_reaction_detail(self, reaction_map: dict):
        """Show an alert dialog listing all reactions and who gave them."""
        dlg = Adw.AlertDialog(
            heading="Reactions",
            body="")
        dlg.add_response("close", "Close")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(4);   box.set_margin_bottom(4)

        for code, info in reaction_map.items():
            row = Gtk.Box(spacing=8)
            emoji_lbl = Gtk.Label(label=code)
            emoji_lbl.set_xalign(0)
            row.append(emoji_lbl)

            names = []
            for uid in info["uids"]:
                if uid == self._me:
                    names.append("You")
                else:
                    name = self._win.get_user_name(uid) if hasattr(self._win, "get_user_name") else uid
                    names.append(name)
            names_lbl = Gtk.Label(label=", ".join(sorted(names)))
            names_lbl.set_xalign(0)
            names_lbl.set_wrap(True)
            names_lbl.add_css_class("dim-label")
            row.append(names_lbl)
            box.append(row)

        dlg.set_extra_child(box)
        dlg.present(self._win)

    def _refresh_from_server(self):
        """Re-fetch this single message from the API and re-render reactions."""
        gid = self._gid
        mid = self._msg["id"]
        def worker():
            msgs = self._api.get_messages(gid, before_id=str(int(mid) + 1), limit=1)
            for m in msgs:
                if str(m.get("id")) == str(mid):
                    GLib.idle_add(self.refresh, m)
                    return
        threading.Thread(target=worker, daemon=True).start()

    def refresh(self, updated_msg: dict):
        """Update reactions in-place from a freshly-fetched server message."""
        self._msg = updated_msg
        self._render_reactions(
            updated_msg.get("reactions", []),
            updated_msg.get("favorited_by", [])
        )


# ─────────────────────────── Chat View ───────────────────────────

