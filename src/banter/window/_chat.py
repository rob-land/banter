"""ChatMixin — open a chat / DM in the content pane + register the
per-conversation window actions backing each header menu.

Mixed into BanterWindow. Owns the construction of the per-chat
HeaderBar (title widget, mute, call, find, view-more menu),
mounting the ChatView, and clearing previous chat state.
"""

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..constants import esc
from ..dialogs.events import CreateEventDialog, CreatePollDialog, EventsListDialog
from ..dialogs.gallery import GalleryDialog
from ..dialogs.group import ContactDetailDialog, NewGroupDialog
from ..dialogs.jump_to_date import JumpToDateDialog
from ..dialogs.pinned import PinnedDialog
from ..helpers import set_avatar_from_url
from ..widgets.chat_view import ChatView


class ChatMixin:
    def _open_chat(self, group: dict):
        self._clear_content()

        hdr = Adw.HeaderBar()
        hdr.add_css_class("flat")
        # No manual back button — Adw.NavigationSplitView automatically injects
        # one into this HeaderBar when the view is collapsed (mobile mode).

        # Title: avatar + name
        tw = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        av = Adw.Avatar(size=30, text=esc(group.get("name","G")),
                        show_initials=True)
        set_avatar_from_url(av, group.get("image_url",""))
        tw.append(av)
        tl = Gtk.Label(label=esc(group.get("name","Group")))
        tl.add_css_class("heading")
        tl.set_ellipsize(Pango.EllipsizeMode.END)
        tl.set_max_width_chars(22)
        tw.append(tl)
        hdr.set_title_widget(tw)

        # Group action menu (Members first for easy access on phone)
        grp_menu = Gio.Menu()
        grp_menu.append("Members",       "win.grp-members")
        grp_menu.append("Pinned",        "win.grp-pinned")
        grp_menu.append("Jump to Date",  "win.grp-jump-date")
        grp_menu.append("Gallery",       "win.grp-album")
        grp_menu.append("View Events",   "win.grp-events-view")
        grp_menu.append("Create Event",  "win.grp-event")
        grp_menu.append("Add Poll",      "win.grp-poll")
        grp_menu.append("Share Group",   "win.grp-share")
        grp_menu.append("Settings",      "win.grp-settings")

        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        menu_btn.add_css_class("flat")
        menu_btn.set_tooltip_text("Group actions")
        menu_btn.set_menu_model(grp_menu)
        hdr.pack_end(menu_btn)

        # Search-in-conversation toggle (also bound to Ctrl+F).
        find_btn = Gtk.Button(icon_name="system-search-symbolic")
        find_btn.add_css_class("flat")
        find_btn.set_tooltip_text("Search this chat (Ctrl+F)")
        find_btn.set_action_name("win.find")
        hdr.pack_end(find_btn)

        # Mute menu. Bell when notifications are on; bell-with-slash
        # when the conv is muted. Tooltip flips to match.
        mute_btn = Gtk.MenuButton()
        mute_btn.add_css_class("flat")
        self._refresh_mute_button(mute_btn, "group", group["id"])
        mute_btn.set_menu_model(
            self._build_mute_menu("group", str(group["id"])))
        self._mute_btn = mute_btn
        hdr.pack_end(mute_btn)

        # Start / join call. GroupMe calls are Microsoft Teams meetings
        # under the hood; we just open the meeting URL in the browser
        # and let Teams handle the media.
        call_btn = Gtk.Button(icon_name="call-start-symbolic")
        call_btn.add_css_class("flat")
        call_btn.set_tooltip_text("Start or join call")
        call_btn.connect("clicked", self._on_call_clicked, group["id"])
        hdr.pack_end(call_btn)

        self.content_nav.set_title(esc(group.get("name","Group")))
        self.content_tv.add_top_bar(hdr)
        self._content_header = hdr

        # Register per-group window actions
        self._register_group_actions(group)

        cv = ChatView(
            self._api, group,
            self._current_user.get("id"),
            self,
            config=self._config,
        )
        cv.set_vexpand(True)
        self.content_wrap.append(cv)
        self._clear_poll_cards()
        self._chat_view = cv

    def _new_group(self):
        NewGroupDialog(self._api, self).present(self)

    # ── Open DM conversation ──
    def open_dm_for_user(self, user: dict, user_id: str):
        """Public entry point — open a DM with any user by ID.
        Can be called from MessageBubble, MembersDialog, ContactRow, etc."""
        if not user_id:
            self.toast("Cannot open DM: no user ID")
            return
        self._open_dm(user, str(user_id))
        self.split.set_show_content(True)

    # ── Contact detail (Android-style profile sheet) ──
    def open_contact_detail(self, user: dict, user_id: str):
        """Open the contact detail dialog showing mutual groups, actions,
        and block/report menu. Called when a user avatar is tapped."""
        if not user_id:
            return
        me_id = (self._current_user or {}).get("id", "")
        # Use _all_groups_with_members if loaded; fall back to the
        # member-less list (mutual detection will simply come back empty
        # until contact load finishes).
        groups = self._all_groups_with_members or self._all_groups
        ContactDetailDialog(
            self._api, user, str(user_id),
            groups, me_id, self,
        ).present(self)

    def _open_dm(self, other_user: dict, other_user_id: str):
        """Open a ChatView configured for a direct message thread."""
        self._clear_content()
        name = other_user.get("name") or other_user.get("nickname", "Direct Message")

        hdr = Adw.HeaderBar()
        hdr.add_css_class("flat")
        # No manual back button — NavigationSplitView injects one automatically.

        tw = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        av = Adw.Avatar(size=30, text=esc(name), show_initials=True)
        set_avatar_from_url(av, other_user.get("avatar_url", ""))
        tw.append(av)
        tl = Gtk.Label(label=esc(name))
        tl.add_css_class("heading")
        tl.set_ellipsize(Pango.EllipsizeMode.END)
        tl.set_max_width_chars(22)
        tw.append(tl)
        hdr.set_title_widget(tw)

        # DM action menu — mirrors the group's view-more button.
        dm_menu = Gio.Menu()
        dm_menu.append("Pinned",          "win.dm-pinned")
        dm_menu.append("Jump to Date",    "win.dm-jump-date")
        dm_menu.append("Contact Details", "win.dm-contact")

        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        menu_btn.add_css_class("flat")
        menu_btn.set_tooltip_text("Conversation actions")
        menu_btn.set_menu_model(dm_menu)
        hdr.pack_end(menu_btn)

        find_btn = Gtk.Button(icon_name="system-search-symbolic")
        find_btn.add_css_class("flat")
        find_btn.set_tooltip_text("Search this chat (Ctrl+F)")
        find_btn.set_action_name("win.find")
        hdr.pack_end(find_btn)

        mute_btn = Gtk.MenuButton()
        mute_btn.add_css_class("flat")
        self._refresh_mute_button(mute_btn, "dm", other_user_id)
        mute_btn.set_menu_model(
            self._build_mute_menu("dm", str(other_user_id)))
        self._mute_btn = mute_btn
        hdr.pack_end(mute_btn)

        call_btn = Gtk.Button(icon_name="call-start-symbolic")
        call_btn.add_css_class("flat")
        call_btn.set_tooltip_text("Start or join call")
        call_btn.connect("clicked", self._on_call_clicked,
                          str(other_user_id))
        hdr.pack_end(call_btn)

        self.content_nav.set_title(esc(name))
        self.content_tv.add_top_bar(hdr)
        self._content_header = hdr

        self._register_dm_actions(other_user, other_user_id)

        # Synthetic "group" dict so ChatView has something for _gid
        # (only used for UI display; actual API calls use other_user_id)
        fake_group = {"id": other_user_id, "name": name}

        cv = ChatView(
            self._api, fake_group,
            self._current_user.get("id"),
            self,
            is_dm=True,
            other_user_id=other_user_id,
            config=self._config,
        )
        cv.set_vexpand(True)
        self.content_wrap.append(cv)
        self._clear_poll_cards()
        self._chat_view = cv

    def _register_group_actions(self, group: dict):
        """Register Gio.SimpleActions on the window for the current group menu."""
        win = self

        def _remove_action(name):
            try:
                win.remove_action(name)
            except Exception:
                pass

        for name in ("set-mute", "grp-members", "grp-pinned", "grp-jump-date",
                     "grp-album", "grp-events-view", "grp-event", "grp-poll",
                     "grp-share", "grp-settings"):
            _remove_action(name)

        def _act(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            win.add_action(a)

        _act("grp-members",     lambda: self._show_members_panel())
        _act("grp-pinned",      lambda: PinnedDialog(self._api, self, group=group).present(self))
        _act("grp-jump-date",   lambda: JumpToDateDialog(self).present(self))
        _act("grp-album",       lambda: GalleryDialog(self._api, group, self).present(self))
        me_id = (self._current_user or {}).get("id", "")
        _act("grp-events-view", lambda: EventsListDialog(self._api, group, me_id, self).present(self))
        _act("grp-event",       lambda: CreateEventDialog(self._api, group, self).present(self))
        _act("grp-poll",     lambda: CreatePollDialog(self._api, group, self).present(self))
        _act("grp-share",    lambda: self._share_group(group))
        _act("grp-settings", lambda: self._open_group_settings(group))

        gid = str(group["id"])
        mute_act = Gio.SimpleAction.new("set-mute", GLib.VariantType.new("i"))
        mute_act.connect("activate",
            lambda _a, p: self._apply_mute("group", gid, p.get_int32()))
        win.add_action(mute_act)

    def _register_dm_actions(self, other_user: dict, other_user_id: str):
        """Register window actions backing the DM header's view-more menu."""
        win = self
        for name in ("set-mute", "dm-pinned", "dm-jump-date", "dm-contact"):
            try:
                win.remove_action(name)
            except Exception:
                pass

        def _act(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            win.add_action(a)

        _act("dm-pinned",    lambda: PinnedDialog(
            self._api, self,
            other_user_id=str(other_user_id),
            other_user_name=(other_user.get("name") or "")
        ).present(self))
        _act("dm-jump-date", lambda: JumpToDateDialog(self).present(self))
        _act("dm-contact",   lambda: self.open_contact_detail(
            other_user, str(other_user_id)))

        oid = str(other_user_id)
        mute_act = Gio.SimpleAction.new("set-mute", GLib.VariantType.new("i"))
        mute_act.connect("activate",
            lambda _a, p: self._apply_mute("dm", oid, p.get_int32()))
        win.add_action(mute_act)
