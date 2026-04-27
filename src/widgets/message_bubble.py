"""Banter — MessageBubble widget with inline reactions."""

import re
from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gdk, Pango

from ..constants import DEBUG, esc, EMOJI_LOG
from ..async_utils import run_in_background
from ..api import GroupMeAPI
from ..helpers import set_avatar_from_url, set_pack_emoji
from .misc import ImageAttachment
from .event_card import EventCard

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
        self.msg  = msg
        self.gid  = group_id
        self.api  = api
        self.win  = window
        self.me   = str(me_id)
        is_mine    = str(msg.get("user_id", "")) == self.me

        # Cache this sender's name so reaction tooltips can resolve it
        uid  = str(msg.get("user_id", ""))
        name = msg.get("name", "")
        if uid and name:
            window.cache_sender_name(uid, name)

        # ── Sender header (others only) ──
        if not is_mine:
            hdr = Gtk.Box(spacing=6)
            av  = Adw.Avatar(size=28,
                             text=esc(msg.get("name", "?")),
                             show_initials=True)
            set_avatar_from_url(av, msg.get("avatar_url"))
            hdr.append(av)

            nm = Gtk.Label(label=esc(msg.get("name", "Unknown")))
            nm.add_css_class("dim-caption")
            nm.add_css_class("bold-name")
            nm.set_halign(Gtk.Align.START)
            nm.set_ellipsize(Pango.EllipsizeMode.END)
            nm.set_max_width_chars(28)
            hdr.append(nm)

            ts  = msg.get("created_at", 0)
            dt  = datetime.fromtimestamp(ts)
            tl  = Gtk.Label(label=dt.strftime("%-I:%M %p"))
            tl.add_css_class("dim-caption")
            tl.add_css_class("dim-label")
            tl.set_margin_start(4)
            hdr.append(tl)

            hdr.set_margin_start(4)

            # Click avatar (only) → open the contact detail dialog.
            # Restricting to the avatar stops accidental taps while
            # scrolling a group chat on a phone.
            sender_uid  = str(msg.get("user_id", ""))
            sender_info = {
                "name"      : msg.get("name", ""),
                "avatar_url": msg.get("avatar_url", ""),
                "user_id"   : sender_uid,
            }
            av.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            gest = Gtk.GestureClick()
            gest.connect("pressed",
                         lambda *_, u=sender_uid, s=sender_info:
                             window.open_contact_detail(s, u))
            av.add_controller(gest)
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
                run_in_background(_fetch_parent)

        text = (msg.get("text") or "").strip()

        # Pre-scan attachments for a pack-emoji entry; if present, the
        # text needs to render as an inline mix of labels and images
        # rather than a single Label.
        emoji_att = next(
            (a for a in msg.get("attachments", []) if a.get("type") == "emoji"),
            None,
        )
        used_mixed_text = False
        if text and emoji_att:
            placeholder = emoji_att.get("placeholder") or ""
            charmap     = emoji_att.get("charmap") or []
            if placeholder and charmap:
                mixed = self._build_pack_emoji_row(text, placeholder, charmap)
                if mixed is not None:
                    bubble.append(mixed)
                    used_mixed_text = True

        if text and not used_mixed_text:
            markup, has_links = _linkify(text)
            lbl = Gtk.Label(wrap=True)
            lbl.set_xalign(0)
            lbl.set_selectable(True)
            lbl.set_max_width_chars(45)
            # WORD_CHAR wrapping lets long URLs/unbreakable tokens break
            # mid-string instead of forcing the label to demand the full
            # content width. This is what prevents the "header disappears"
            # bug without collapsing the bubble to 1px.
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if has_links:
                # Use markup so <a href> links are rendered as clickable
                lbl.set_markup(markup)
                lbl.set_use_markup(True)
                # GTK label handles link activation natively when use_markup=True
            else:
                lbl.set_text(text)
            bubble.append(lbl)

        for att in msg.get("attachments", []):
            kind = att.get("type", "")
            if kind in ("reply", "emoji"):
                continue   # reply → quote block; emoji → handled above
            if kind == "image":
                img = ImageAttachment(att["url"], window)
                bubble.append(img)
            elif kind == "location":
                loc = Gtk.Label(
                    label=f"📍 {att.get('name','Location')}"
                          f"\n{att.get('lat')}, {att.get('lng')}")
                loc.set_wrap(True)
                loc.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                loc.set_max_width_chars(45)
                loc.set_xalign(0)
                bubble.append(loc)
            elif kind == "split":
                lbl = Gtk.Label(label="💳 Split request")
                bubble.append(lbl)
            elif kind.startswith("event"):
                # GroupMe shares the "event" attachment across several
                # system messages — creation, "is going", "is not going",
                # updates. Only the "created event" post should render a
                # card; the RSVP follow-ups would otherwise duplicate a
                # card for every response, cluttering the chat.
                if "created event" in text.lower():
                    event_id   = att.get("event_id") or att.get("id")
                    event_data = att.get("event") if isinstance(att.get("event"), dict) else None
                    if event_id or event_data:
                        card = EventCard(api, group_id, me_id,
                                         event_id, event_data, window)
                        bubble.append(card)

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
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_lbl.set_max_width_chars(28)
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
        text_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text_lbl.set_max_width_chars(40)
        self._quote_box.append(text_lbl)

    # ── Reactions ──
    def _render_reactions(self, reactions: list, favorited_by: list):
        """Rebuild the reactions row from server data.

        All interactions — adding, removing, switching, seeing reactors —
        are consolidated into a single ReactionsSheet. Clicking either a
        pill or the add-reaction button opens the sheet. This avoids the
        old mix of tooltip / tap-pill-to-switch / tap-empty-row-to-inspect
        affordances that didn't translate to touch."""
        # Clear
        child = self._reactions_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._reactions_box.remove(child)
            child = nxt

        # Build reaction_map: unique key → metadata.
        # For pack reactions, key includes pack_id+offset so multiple
        # distinct pack emojis don't collapse into a single pill.
        reaction_map = {}
        for r in (reactions or []):
            r_type = r.get("type", "unicode")
            if r_type == "emoji":
                pack_id = r.get("pack_id")
                # GroupMe responses use either `offset` or `pack_index`
                # depending on endpoint/era — accept both.
                offset  = r.get("offset")
                if offset is None:
                    offset = r.get("pack_index")
                key     = f"pack:{pack_id}:{offset}"
                entry = {
                    "uids"     : set(),
                    "raw_code" : r.get("placeholder") or "😊",
                    "is_pack"  : True,
                    "pack_id"  : pack_id,
                    "offset"   : offset,
                }
                # Capture pack reaction shapes for schema investigation.
                # Gated on DEBUG so a chat with many pack reactions
                # doesn't do synchronous disk writes on every re-render.
                if DEBUG:
                    try:
                        with open(EMOJI_LOG, "a") as f:
                            f.write(f"{datetime.now().isoformat()}  msg={self.msg.get('id')}  {r}\n")
                    except Exception:
                        pass
            else:
                code = r.get("code") or "❤️"
                key  = code
                entry = {
                    "uids"     : set(),
                    "raw_code" : code,
                    "is_pack"  : False,
                }
            uid_list = r.get("user_ids", [])
            if key not in reaction_map:
                reaction_map[key] = entry
            reaction_map[key]["uids"].update(str(u) for u in uid_list)

        # Legacy heart likes fallback
        if favorited_by and not reaction_map:
            uids = set(str(u) for u in favorited_by)
            reaction_map["❤️"] = {"uids": uids, "raw_code": "❤️", "is_pack": False}

        me = self.me

        # Reaction pills
        for _, info in reaction_map.items():
            uid_set = info["uids"]
            count   = len(uid_set)
            i_used  = me in uid_set

            pill = Gtk.Button()
            pill.add_css_class("flat")
            pill.add_css_class("reaction-pill")
            if i_used:
                pill.add_css_class("reaction-pill-mine")

            pill_content = Gtk.Box(spacing=4)
            if info.get("is_pack"):
                emoji_widget = Gtk.Image()
                emoji_widget.set_pixel_size(18)
                pid, off = info.get("pack_id"), info.get("offset")
                if pid is not None and off is not None:
                    set_pack_emoji(emoji_widget, pid, off, 18)
                pill_content.append(emoji_widget)
            else:
                emoji_lbl = Gtk.Label(label=info.get("raw_code", ""))
                pill_content.append(emoji_lbl)
            count_lbl = Gtk.Label(label=str(count))
            pill_content.append(count_lbl)
            pill.set_child(pill_content)

            pill.connect("clicked",
                         lambda *_, m=reaction_map: self._open_reactions_sheet(m))
            self._reactions_box.append(pill)

        # Add-reaction button — always shown so the user can change or
        # add new reactions even when they've already reacted.
        add_btn = self._make_add_reaction_btn()
        add_btn.connect("clicked",
                        lambda *_, m=reaction_map: self._open_reactions_sheet(m))
        self._reactions_box.append(add_btn)

    def _open_reactions_sheet(self, reaction_map: dict):
        try:
            from .reactions_sheet import ReactionsSheet
            sheet = ReactionsSheet(self, reaction_map)
            sheet.present(self.win)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            # Always print to stderr so the user running from a terminal
            # can see the full stack, regardless of --debug.
            import sys
            print("=== ReactionsSheet failed ===", file=sys.stderr)
            print(tb, file=sys.stderr)
            print("=============================", file=sys.stderr)
            self.win.toast(f"Reactions unavailable: {e}")

    def _build_pack_emoji_row(self, text: str, placeholder: str, charmap: list):
        """Build an inline text+image layout for a message carrying a
        pack-emoji attachment.

        GroupMe places a placeholder unicode character in `text` at each
        position where a pack emoji should appear; `charmap` supplies
        (pack_id, offset) pairs in order. We split the text at placeholder
        occurrences and weave in Gtk.Image widgets.

        Returns a Gtk.FlowBox-based widget that wraps reasonably on narrow
        screens. For pure emoji messages (text is only placeholders) this
        is a compact row of images; for mixed messages, text and images
        flow together word-by-word."""
        if not placeholder or not charmap:
            return None

        # Split the text at placeholder boundaries, preserving the gaps.
        segments = []
        last = 0
        emoji_idx = 0
        i = 0
        while i < len(text):
            if text[i] == placeholder:
                if i > last:
                    segments.append(("text", text[last:i]))
                if emoji_idx < len(charmap):
                    pair = charmap[emoji_idx]
                    pack_id = pair[0] if len(pair) > 0 else None
                    offset  = pair[1] if len(pair) > 1 else None
                    segments.append(("emoji", pack_id, offset))
                emoji_idx += 1
                last = i + 1
            i += 1
        if last < len(text):
            segments.append(("text", text[last:]))

        # Nothing to mix — caller will fall back to the plain text label.
        if not any(s[0] == "emoji" for s in segments):
            return None

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_column_spacing(2)
        flow.set_row_spacing(2)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(64)
        flow.set_homogeneous(False)
        flow.set_halign(Gtk.Align.START)

        for seg in segments:
            if seg[0] == "text":
                # Split text segments on whitespace so the FlowBox can
                # wrap between words instead of forcing the full segment
                # onto one line.
                chunk = seg[1]
                for word in chunk.split(" "):
                    if not word:
                        continue
                    lbl = Gtk.Label(label=word)
                    lbl.set_xalign(0)
                    flow.append(lbl)
            else:
                _, pack_id, offset = seg
                img = Gtk.Image()
                img.set_pixel_size(22)
                if pack_id is not None and offset is not None:
                    set_pack_emoji(img, pack_id, offset, 22)
                flow.append(img)
        return flow

    def _make_add_reaction_btn(self):
        """Compact add-reaction button: a symbolic smiley (takes the
        label text color) next to a small '+' glyph.

        We avoid Gtk.Overlay here — it was eating click events on the
        Button in practice, leaving the control unresponsive."""
        box = Gtk.Box(spacing=1)
        box.set_valign(Gtk.Align.CENTER)

        smiley = Gtk.Image.new_from_icon_name("face-smile-symbolic")
        smiley.set_pixel_size(14)
        box.append(smiley)

        plus = Gtk.Label(label="+")
        plus.add_css_class("reaction-add-plus")
        plus.set_valign(Gtk.Align.CENTER)
        box.append(plus)

        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("reaction-add-btn")
        btn.set_child(box)
        btn.set_tooltip_text("Add reaction")
        return btn

    def refresh_from_server(self):
        """Re-fetch this single message from the API and re-render reactions."""
        gid = self.gid
        mid = self.msg["id"]
        def worker():
            msgs = self.api.get_messages(gid, before_id=str(int(mid) + 1), limit=1)
            for m in msgs:
                if str(m.get("id")) == str(mid):
                    GLib.idle_add(self.refresh, m)
                    return
        run_in_background(worker)

    def refresh(self, updated_msg: dict):
        """Update reactions in-place from a freshly-fetched server message."""
        self.msg = updated_msg
        self._render_reactions(
            updated_msg.get("reactions", []),
            updated_msg.get("favorited_by", [])
        )


# ─────────────────────────── Chat View ───────────────────────────

