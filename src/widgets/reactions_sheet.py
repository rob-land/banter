"""Banter — ReactionsSheet: unified picker + reactors view for a message.

Layout (top → bottom):

  ┌───────────────────────────────────────┐
  │ [header bar]              Reactions   │
  ├───────────────────────────────────────┤
  │ [quick row of popular emoji ▸ scroll] │   always visible, horizontal-only
  ├───────────────────────────────────────┤
  │                                       │
  │ Emoji                                 │
  │ [grid of unicode reactions]           │
  │                                       │
  │ Pack name 1                           │   one section per loaded powerup
  │ [grid of pack emoji images]           │
  │                                       │
  │ Pack name 2                           │
  │ ...                                   │
  │                                       │
  │ Given                                 │   who reacted with what
  │ [list of current reactions + names]   │
  │                                       │
  ├───────────────────────────────────────┤
  │ [😊] [📦] [📦] [📦] ...               │   category nav — jumps main
  └───────────────────────────────────────┘   scroll to the section

Picking any emoji toggles / replaces the user's reaction on the message.
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, GLib

from ..constants import DEFAULT_REACTIONS, esc
from ..async_utils import run_in_background
from ..helpers import get_all_packs, set_pack_emoji, load_image_async, pack_info


class ReactionsSheet(Adw.Dialog):
    """Unified reactions sheet for a single message."""

    CELL     = 40     # main picker cell size
    NAV_CELL = 36     # bottom category-nav cell size

    def __init__(self, bubble, reaction_map: dict):
        super().__init__()
        self._bubble       = bubble
        self._api          = bubble.api
        self._gid          = bubble.gid
        self._msg_id       = bubble.msg["id"]
        self._me           = bubble.me
        self._reaction_map = reaction_map

        # The user's current reaction key, or None.
        self._my_key = next(
            (k for k, v in reaction_map.items() if self._me in v["uids"]),
            None,
        )

        # section-key → wrapper widget (for the category-nav scroll-to)
        self._sections: dict = {}
        self._main_body   = None
        self._main_scroll = None

        self.set_title("Reactions")
        self.set_content_width(420)
        self.set_content_height(600)

        # Track packs that actually got rendered so the category nav
        # only shows buttons that scroll to real content.
        self._pack_order: list = []   # [(pack_id, pack_dict), ...]

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self._build_main_scroll())

        if self._pack_order:
            tv.add_bottom_bar(self._build_category_nav(self._pack_order))

        tv.set_content(outer)
        self.set_child(tv)

    # ── Picker button helpers ────────────────────────────────────────
    def _make_unicode_btn(self, code: str):
        btn = Gtk.Button(label=code)
        btn.add_css_class("flat")
        btn.add_css_class("reaction-picker-btn")
        btn.set_size_request(self.CELL, self.CELL)
        if self._my_key == code:
            btn.add_css_class("reaction-picker-mine")
        btn.connect("clicked", self._on_unicode_pick, code)
        return btn

    def _make_pack_btn(self, pack_id, offset: int):
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("reaction-picker-btn")
        btn.set_size_request(self.CELL, self.CELL)

        img = Gtk.Image()
        img.set_pixel_size(28)
        set_pack_emoji(img, pack_id, offset, 28)
        btn.set_child(img)

        key = f"pack:{pack_id}:{offset}"
        if self._my_key == key:
            btn.add_css_class("reaction-picker-mine")
        btn.connect("clicked", self._on_pack_pick, pack_id, offset)
        return btn

    # ── Main scrollable picker (vertical) ────────────────────────────
    def _build_main_scroll(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_kinetic_scrolling(True)
        self._main_scroll = scroll

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        body.set_margin_start(16); body.set_margin_end(16)
        body.set_margin_top(12);   body.set_margin_bottom(16)
        self._main_body = body

        # Given reactions + reactors — at the top so existing responses
        # are visible without scrolling; picker sections are below.
        given = self._build_given_section()
        if given is not None:
            body.append(given)

        # Unicode section — all of DEFAULT_REACTIONS
        body.append(self._build_unicode_section())

        # One section per loaded powerup pack — record those that
        # actually rendered so the bottom nav can be built off them.
        for pack in get_all_packs():
            if not isinstance(pack, dict):
                continue
            result = self._build_pack_section(pack)
            if result is not None:
                section, info = result
                body.append(section)
                self._pack_order.append(info)

        scroll.set_child(body)
        return scroll

    def _grid_section(self, key: str, title: str):
        """Shared frame + heading + FlowBox for a picker section."""
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        lbl = Gtk.Label(label=title, xalign=0)
        lbl.add_css_class("heading")
        section.append(lbl)

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_column_spacing(4)
        flow.set_row_spacing(4)
        flow.set_min_children_per_line(6)
        flow.set_max_children_per_line(10)
        flow.set_homogeneous(True)
        section.append(flow)

        self._sections[key] = section
        return section, flow

    def _build_unicode_section(self):
        section, flow = self._grid_section("unicode", "Emoji")
        for code in DEFAULT_REACTIONS:
            flow.append(self._make_unicode_btn(code))
        return section

    def _build_pack_section(self, pack: dict):
        """Build a grid section for one powerup pack.

        Returns (section, pack_info_dict) or None when the pack isn't
        renderable (e.g. missing sprite variant or zero emojis)."""
        info = pack_info(pack)
        if info is None:
            return None

        key = str(info["pack_id"])
        # Render every emoji in the pack — an 80-cell cap previously
        # truncated anything past offset 79 (e.g. the Greek letters in
        # Back to School, which live at offsets 77–100).
        section, flow = self._grid_section(key, esc(info["name"]))
        for offset in range(info["pack_size"]):
            flow.append(self._make_pack_btn(info["pack_id"], offset))
        return section, info

    def _build_given_section(self):
        if not self._reaction_map:
            return None

        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        lbl = Gtk.Label(label="Given", xalign=0)
        lbl.add_css_class("heading")
        section.append(lbl)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")

        win = self._bubble.win
        for _, info in self._reaction_map.items():
            uids  = info["uids"]
            count = len(uids)
            row = Adw.ActionRow()
            row.set_title(f"  ×  {count}")

            if info.get("is_pack"):
                img = Gtk.Image()
                img.set_pixel_size(22)
                pack_id = info.get("pack_id")
                offset  = info.get("offset")
                if pack_id is not None and offset is not None:
                    set_pack_emoji(img, pack_id, offset, 22)
                row.add_prefix(img)
            else:
                pill = Gtk.Label(label=info.get("raw_code", ""))
                pill.add_css_class("title-3")
                row.add_prefix(pill)

            names = []
            for uid in uids:
                if uid == self._me:
                    names.append("You")
                else:
                    name = win.get_user_name(uid)
                    names.append(name)
            row.set_subtitle(esc(", ".join(sorted(names))))
            row.set_subtitle_lines(3)

            list_box.append(row)

        section.append(list_box)
        self._sections["given"] = section
        return section

    # ── Category nav (bottom bar) ────────────────────────────────────
    def _build_category_nav(self, pack_order):
        """Horizontal icon bar — one button per section that actually
        rendered in the main scroll. pack_order is [info_dict, ...]
        in the same order the sections appear in the main body, where
        each info_dict comes from helpers.pack_info()."""
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroll.set_hexpand(True)
        scroll.set_kinetic_scrolling(True)
        scroll.add_css_class("reaction-category-nav")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.set_margin_top(4)
        row.set_margin_bottom(4)

        # Unicode category
        emoji_btn = Gtk.Button()
        emoji_btn.add_css_class("flat")
        emoji_btn.set_icon_name("face-smile-symbolic")
        emoji_btn.set_size_request(self.NAV_CELL, self.NAV_CELL)
        emoji_btn.set_tooltip_text("Emoji")
        emoji_btn.connect("clicked", lambda *_: self._scroll_to_section("unicode"))
        row.append(emoji_btn)

        # One button per rendered pack
        for info in pack_order:
            key      = str(info["pack_id"])
            name     = info["name"]
            icon_url = info.get("icon_url")

            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_size_request(self.NAV_CELL, self.NAV_CELL)
            btn.set_tooltip_text(name)

            img = Gtk.Image()
            img.set_pixel_size(24)
            if icon_url:
                def _on_loaded(path, w=img):
                    if path:
                        w.set_from_file(path)
                load_image_async(icon_url, _on_loaded)
            else:
                # No dedicated icon — fall back to the pack's first emoji.
                set_pack_emoji(img, info["pack_id"], 0, 24)
            btn.set_child(img)

            btn.connect("clicked",
                        lambda *_, k=key: self._scroll_to_section(k))
            row.append(btn)

        scroll.set_child(row)
        return scroll

    def _scroll_to_section(self, key: str):
        """Scroll the main picker so `key`'s section is at the top."""
        section = self._sections.get(key)
        if section is None or self._main_scroll is None or self._main_body is None:
            return
        # compute_bounds is GTK4's replacement for get_allocation — it
        # returns the widget's rect in the coordinate space of an
        # ancestor. We want it relative to the scrollable body so we can
        # feed it straight into the vadjustment.
        ok, rect = section.compute_bounds(self._main_body)
        if not ok:
            return
        adj = self._main_scroll.get_vadjustment()
        adj.set_value(max(0.0, rect.origin.y))

    # ── Pick handlers ────────────────────────────────────────────────
    def _on_unicode_pick(self, _btn, code):
        self._apply_pick(new_key=code, new_code=code, is_pack=False,
                         pack_id=None, offset=None)

    def _on_pack_pick(self, _btn, pack_id, offset):
        key = f"pack:{pack_id}:{offset}"
        self._apply_pick(new_key=key, new_code=None, is_pack=True,
                         pack_id=pack_id, offset=offset)

    def _apply_pick(self, *, new_key, new_code, is_pack, pack_id, offset):
        gid, mid = self._gid, self._msg_id
        old_key = self._my_key
        removing_only = (old_key == new_key)

        def worker():
            if old_key is not None:
                self._api.unreact_message(gid, mid)
            if removing_only:
                GLib.idle_add(self._after_apply)
                return
            if is_pack:
                ok, hint = self._api.react_message_pack(
                    gid, mid, pack_id, offset)
                label = f"pack {pack_id}/{offset}"
            else:
                ok = self._api.react_message(gid, mid, new_code)
                hint = None
                label = new_code or "that reaction"
            if ok:
                GLib.idle_add(self._after_apply)
            else:
                GLib.idle_add(self._flash_react_failed, label, hint)
        run_in_background(worker)

        self.close()

    def _after_apply(self):
        self._bubble.refresh_from_server()

    def _flash_react_failed(self, label, hint=None):
        msg = f"GroupMe rejected {label}"
        if hint:
            msg += f" — {hint}"
        self._bubble.win.toast(msg)
        self._after_apply()
