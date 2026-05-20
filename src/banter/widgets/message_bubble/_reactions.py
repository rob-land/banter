"""ReactionsMixin — reaction-pill rendering, add-reaction button,
pack-emoji inline layout, and the ReactionsSheet dispatch.

Mixed into MessageBubble. All methods access instance state
(`self._reactions_box`, `self.is_mine`, `self.me`, `self.msg`,
`self.win`, etc.) set up by the host class's `__init__`.
"""

import sys
import traceback
from datetime import datetime

from gi.repository import Gtk

from ...constants import DEBUG, EMOJI_LOG
from ...helpers import set_pack_emoji


class ReactionsMixin:
    def _build_reactions_row(self):
        """Create and append the reactions Gtk.Box. Idempotent — no-op
        if the row already exists (e.g. transition_to_sent re-calling
        on an already-built bubble)."""
        if self._reactions_box is not None:
            return
        self._reactions_box = Gtk.Box(spacing=4)
        self._reactions_box.set_halign(
            Gtk.Align.END if self.is_mine else Gtk.Align.START)
        self._reactions_box.set_margin_start(8)
        self._reactions_box.set_margin_end(8)
        self._reactions_box.set_margin_top(2)
        self.append(self._reactions_box)

    def _render_reactions(self, reactions: list, favorited_by: list):
        """Rebuild the reactions row from server data.

        All interactions — adding, removing, switching, seeing reactors —
        are consolidated into a single ReactionsSheet. Clicking either a
        pill or the add-reaction button opens the sheet. This avoids the
        old mix of tooltip / tap-pill-to-switch / tap-empty-row-to-inspect
        affordances that didn't translate to touch."""
        if self._reactions_box is None:
            return   # pending bubble — nothing to render
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
            uids = {str(u) for u in favorited_by}
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
            from ..reactions_sheet import ReactionsSheet
            sheet = ReactionsSheet(self, reaction_map)
            sheet.present(self.win)
        except Exception as e:
            tb = traceback.format_exc()
            # Always print to stderr so the user running from a terminal
            # can see the full stack, regardless of --debug.
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
