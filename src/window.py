"""Banter — MainWindow: the application's primary window."""

import threading
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk

from .constants import APP_NAME, APP_VERSION, DEBUG, dbg, esc
from .config import Config
from .api import GroupMeAPI
from .push import GroupMePush
from .helpers import set_avatar_from_url
from .widgets.conversation_row import ConversationRow, GroupRow, DMRow, ContactRow
from .widgets.chat_view import ChatView
from .oauth import LoginDialog
from .dialogs.accounts import AccountsDialog
from .dialogs.group import GroupDetailDialog, NewGroupDialog, ContactDetailDialog
from .dialogs.members import MembersDialog
from .dialogs.settings import GroupSettingsDialog, PreferencesDialog
from .dialogs.gallery import GalleryDialog
from .dialogs.events import CreateEventDialog, EventsListDialog, CreatePollDialog


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self._config       = Config()
        self._api          = None
        self._current_user = None
        self._current_group= None
        self._chat_view    = None
        self._all_groups   : list = []
        self._all_dms      : list = []
        self._all_contacts : list = []
        # uid → display name cache, built from contacts + message senders
        self._name_cache   : dict = {}
        # Unified chats list (groups + DMs merged, sorted by recency)
        self._all_chats    : list = []
        # Maps conv_id → ConversationRow for live badge/time updates
        self._conv_rows    : dict = {}
        # Keep separate maps for bg-poll compat
        self._group_rows   : dict = {}
        self._dm_rows      : dict = {}
        # Last known message id per conversation (for change detection)
        self._last_msg_ids : dict = {}
        self._bg_poll_id   = None
        self._push         = None   # singleton GroupMePush for the whole session

        self.set_title(APP_NAME)
        self.set_default_size(980, 720)
        self.set_size_request(360, 600)

        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        acc = self._config.get_active_account()
        if acc:
            self._api = GroupMeAPI(acc["token"])
            spinner = Gtk.Spinner(spinning=True, margin_top=120,
                                   halign=Gtk.Align.CENTER)
            self._toast_overlay.set_child(spinner)

            def verify():
                me = self._api.get_me()
                if me:
                    GLib.idle_add(self._build_main_ui, me)
                else:
                    GLib.idle_add(self._go_login)

            threading.Thread(target=verify, daemon=True).start()
        else:
            self._go_login()

    # ── Toast helper ──
    def toast(self, msg: str):
        self._toast_overlay.add_toast(Adw.Toast.new(msg))

    # ── Login ──
    def _go_login(self):
        self._login_dialog = LoginDialog(self, on_login=self._on_login)
        self._login_dialog.present(self)

    def deliver_oauth_token(self, token: str):
        """Called by BanterApplication when the banter:// redirect URI is opened."""
        if hasattr(self, "_login_dialog") and self._login_dialog:
            self._login_dialog.receive_token(token)

    def _on_login(self, token: str, user: dict):
        self._login_dialog = None
        self._api = GroupMeAPI(token)
        self._config.add_account(token, user)
        self._build_main_ui(user)

    # ── Main UI ──
    def _build_main_ui(self, user: dict):
        self._current_user = user

        # Root split view
        self._split = Adw.NavigationSplitView()
        self._split.set_min_sidebar_width(280)
        self._split.set_max_sidebar_width(340)
        self._split.set_sidebar_width_fraction(0.33)

        # ───────── SIDEBAR ─────────
        self._build_sidebar()

        # ───────── CONTENT ─────────
        self._content_nav = Adw.NavigationPage(title=APP_NAME)
        # ToolbarView wraps the content so header bars built by _open_chat /
        # _open_dm sit inside a proper ToolbarView, enabling correct insets,
        # safe-area handling, and integration with NavigationSplitView.
        self._content_tv   = Adw.ToolbarView()
        self._content_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._content_tv.set_content(self._content_wrap)
        self._content_nav.set_child(self._content_tv)
        self._split.set_content(self._content_nav)

        self._show_placeholder()
        self._toast_overlay.set_child(self._split)

        # ── Adaptive breakpoint ──
        # Without this, NavigationSplitView never auto-collapses because
        # the content Gtk.Box reports a minimum width of 0 so the internal
        # threshold (min_sidebar + content_min) is never exceeded.
        # Adw.Breakpoint is the libadwaita-idiomatic way to collapse the
        # split view at a specific window width.
        try:
            condition = Adw.BreakpointCondition.parse("max-width: 600sp")
            bp = Adw.Breakpoint.new(condition)
            bp.add_setter(self._split, "collapsed", True)
            self.add_breakpoint(bp)
            dbg("Breakpoint added: collapse split view at max-width 600sp")
        except Exception as e:
            # Fallback for older libadwaita builds that lack Breakpoint
            dbg("Adw.Breakpoint unavailable (%s) – using width-watch fallback", e)
            self.connect("notify::default-width", self._on_width_change)

        self._setup_actions()
        self.refresh_chats()
        self._load_contacts()
        self._start_bg_poll()
        self._start_push()

    def get_user_name(self, user_id: str) -> str:
        """Return a display name for a user_id.

        Checks in priority order:
          1. _name_cache  — populated from contacts load and message senders
          2. _all_contacts — loaded with full member info
          3. _all_groups members  — usually null (omit=memberships fetch)
        Falls back to the raw user_id string if nothing is found.
        """
        uid = str(user_id)
        if uid in self._name_cache:
            return self._name_cache[uid]
        for c in self._all_contacts:
            if str(c.get("user_id", "")) == uid:
                name = c.get("name") or uid
                self._name_cache[uid] = name
                return name
        for g in self._all_groups:
            for m in (g.get("members") or []):
                if str(m.get("user_id", "")) == uid:
                    name = m.get("nickname") or m.get("name") or uid
                    self._name_cache[uid] = name
                    return name
        return uid

    def cache_sender_name(self, user_id: str, name: str):
        """Record a uid→name mapping seen in a message, for reaction lookups."""
        uid = str(user_id)
        if uid and name and uid not in self._name_cache:
            self._name_cache[uid] = name

    def _on_width_change(self, *_):
        """Fallback collapse logic for libadwaita < 1.4."""
        w = self.get_width()
        self._split.set_collapsed(w < 600)

    # ── Application-level push client (singleton) ──
    def _start_push(self):
        """Create and start the shared push client for this session."""
        if self._push or not self._api:
            return
        user_id = str(self._current_user.get("id", ""))
        if not user_id:
            return
        self._push = GroupMePush(
            self._api.token, user_id,
            on_event=self._on_push_event,
            on_error=lambda m: dbg("push error: %s", m),
        )
        self._push.start()
        dbg("MainWindow: push client started for user %s", user_id)

    def _stop_push(self):
        if self._push:
            self._push.stop()
            self._push = None

    def _on_push_event(self, data: dict):
        """Route push events to the active ChatView (if any)."""
        ev_type = data.get("type", "")
        subject = data.get("subject", {})
        dbg("push event: type=%s", ev_type)

        # Delegate to the currently open ChatView
        if self._chat_view:
            self._chat_view._on_push_event(data)

        # Also keep the sidebar unread counts fresh on new messages
        if ev_type == "line.create":
            gid = str(subject.get("group_id", ""))
            if gid and gid in self._group_rows:
                row = self._group_rows[gid]
                current_gid = (str(self._current_group["id"])
                               if self._current_group else None)
                if gid != current_gid:
                    row.set_unread(row._unread_count.get_text()
                                   and int(row._unread_count.get_text() or 0) + 1
                                   or 1 if row._unread_dot.get_visible() else 1)
                    # Send a real-time desktop notification via push rather than
                    # waiting for the bg_poll (which may fire after the user has
                    # already read the message on another device, making unread=0).
                    sender = subject.get("name", "Someone")
                    text   = (subject.get("text") or "📎 attachment").strip()
                    name   = (self._group_rows[gid].conv or {}).get("name", "GroupMe")
                    self._send_desktop_notification(
                        name, f"{sender}: {text}", tag=f"group-{gid}")
                # Move to top of chats list
                self._chats_list.remove(row)
                self._chats_list.insert(row, 0)
                ts = subject.get("created_at")
                if ts:
                    row.update_time(ts)

    def _build_sidebar(self):
        sidebar_nav = Adw.NavigationPage(title=APP_NAME)

        # Adw.ToolbarView is required for NavigationPage to properly host a
        # header bar inside the NavigationSplitView adaptive navigation stack.
        sidebar_tv = Adw.ToolbarView()

        # Header
        hdr = Adw.HeaderBar()
        hdr.add_css_class("flat")

        ng_btn = Gtk.Button(icon_name="list-add-symbolic")
        ng_btn.add_css_class("flat")
        ng_btn.set_tooltip_text("New Group")
        ng_btn.connect("clicked", lambda *_: self._new_group())
        hdr.pack_end(ng_btn)

        acc_btn = Gtk.MenuButton(icon_name="system-users-symbolic")
        acc_btn.add_css_class("flat")
        acc_btn.set_tooltip_text("Accounts")
        menu = Gio.Menu()
        user = self._current_user
        menu.append(f"Signed in as {user.get('name','')}", None)
        menu.append("Manage Accounts", "app.accounts")
        menu.append("Preferences", "app.preferences")
        menu.append("Sign Out", "app.sign-out")
        acc_btn.set_menu_model(menu)
        hdr.pack_start(acc_btn)

        sidebar_tv.add_top_bar(hdr)

        # Body of the sidebar (search + stack + tab bar)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search groups & contacts…")
        self._search_entry.connect("search-changed", self._on_search)
        self._search_entry.set_margin_start(8)
        self._search_entry.set_margin_end(8)
        self._search_entry.set_margin_bottom(6)
        body.append(self._search_entry)

        # View stack (Chats / Contacts)
        self._stack = Adw.ViewStack()

        # ─ Chats page (groups + DMs merged, sorted by recency) ─
        chats_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._chats_spinner = Gtk.Spinner(spinning=True, margin_top=20,
                                           halign=Gtk.Align.CENTER)
        chats_page.append(self._chats_spinner)
        self._chats_list = Gtk.ListBox()
        self._chats_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._chats_list.add_css_class("navigation-sidebar")
        self._chats_list.connect("row-activated", self._on_chats_activated)
        chats_scroll = Gtk.ScrolledWindow()
        chats_scroll.set_vexpand(True)
        chats_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        chats_scroll.set_child(self._chats_list)
        chats_page.append(chats_scroll)
        self._stack.add_titled_with_icon(
            chats_page, "chats", "Chats", "chat-message-new-symbolic")
        # Keep reference to ViewStackPage for needs-attention dot
        self._chats_page = self._stack.get_page(chats_page)

        # ─ Contacts page ─
        cts_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._contacts_spinner = Gtk.Spinner(spinning=True, margin_top=20,
                                              halign=Gtk.Align.CENTER)
        cts_page.append(self._contacts_spinner)
        self._contacts_list = Gtk.ListBox()
        self._contacts_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._contacts_list.add_css_class("navigation-sidebar")
        self._contacts_list.connect("row-activated", self._on_contact_activated)
        cts_scroll = Gtk.ScrolledWindow()
        cts_scroll.set_vexpand(True)
        cts_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cts_scroll.set_child(self._contacts_list)
        cts_page.append(cts_scroll)
        self._stack.add_titled_with_icon(
            cts_page, "contacts", "Contacts", "address-book-new-symbolic")

        body.append(self._stack)

        tab_bar = Adw.ViewSwitcherBar()
        tab_bar.set_stack(self._stack)
        tab_bar.set_reveal(True)
        body.append(tab_bar)

        sidebar_tv.set_content(body)
        sidebar_nav.set_child(sidebar_tv)
        self._split.set_sidebar(sidebar_nav)

    # ── Placeholder ──
    def _show_placeholder(self):
        self._clear_content()

        # Always show a header bar so the window-close button and the
        # mobile back-navigation button exist before any group is opened.
        hdr = Adw.HeaderBar()
        hdr.add_css_class("flat")
        self._content_wrap.append(hdr)

        page = Adw.StatusPage(
            icon_name="chat-message-new-symbolic",
            title="Welcome to GroupMe",
            description="Select a group from the sidebar to start chatting."
        )
        page.set_vexpand(True)
        self._content_wrap.append(page)
        self._content_nav.set_title(APP_NAME)

    def _clear_content(self):
        if self._chat_view:
            self._chat_view.stop()
            self._chat_view = None
        child = self._content_wrap.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._content_wrap.remove(child)
            child = nxt

    # ── Unified Chats (groups + DMs) ──
    def refresh_chats(self):
        self._chats_spinner.set_spinning(True)
        self._chats_spinner.set_visible(True)

        def worker():
            groups = self._api.get_groups_all()
            chats  = self._api.get_chats_all()
            GLib.idle_add(self._display_chats, groups, chats)

        threading.Thread(target=worker, daemon=True).start()

    # Keep refresh_groups as an alias for code that calls it (e.g. after deleting a group)
    def refresh_groups(self):
        self.refresh_chats()

    def _conv_sort_key(self, conv: dict, conv_type: str) -> int:
        """Return a unix timestamp used to sort conversations newest-first."""
        if conv_type == "dm":
            ts = (conv.get("last_message", {}).get("created_at") or
                  conv.get("updated_at") or 0)
        else:
            ts = conv.get("messages", {}).get("last_message_created_at") or 0
        try:
            return int(ts)
        except (TypeError, ValueError):
            return 0

    def _display_chats(self, groups, chats):
        self._chats_spinner.set_spinning(False)
        self._chats_spinner.set_visible(False)
        self._all_groups = groups
        self._all_dms    = chats
        self._group_rows = {}
        self._dm_rows    = {}
        self._conv_rows  = {}

        # Clear existing rows
        child = self._chats_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._chats_list.remove(child)
            child = nxt

        # Build combined list sorted by most-recent message
        items = (
            [("group", g) for g in groups] +
            [("dm",    c) for c in chats]
        )
        items.sort(key=lambda x: self._conv_sort_key(x[1], x[0]), reverse=True)

        for conv_type, conv in items:
            row = ConversationRow(conv, conv_type, self._config)
            self._chats_list.append(row)

            if conv_type == "group":
                gid = str(conv["id"])
                self._group_rows[gid] = row
                self._conv_rows[gid]  = row
                self._last_msg_ids.setdefault(
                    gid, conv.get("messages", {}).get("last_message_id"))
            else:
                other_id = str(conv.get("other_user", {}).get("id", ""))
                if other_id:
                    self._dm_rows[other_id]        = row
                    self._conv_rows[f"dm_{other_id}"] = row
                    self._last_msg_ids.setdefault(
                        f"dm_{other_id}",
                        conv.get("last_message", {}).get("id"))

        # Update tab attention dot
        total_unread = sum(
            int(g.get("messages", {}).get("unread_count") or 0)
            for g in groups
        ) + sum(
            int(c.get("unread_count") or 0)
            for c in chats
        )
        if hasattr(self, '_chats_page'):
            self._chats_page.set_needs_attention(total_unread > 0)

    def _on_chats_activated(self, lb, row):
        if not isinstance(row, ConversationRow):
            return
        row.set_unread(0)
        if row.conv_type == "group":
            self._current_group = row.conv
            self._open_chat(row.conv)
        else:
            other = row.conv.get("other_user", {})
            other_id = str(other.get("id", ""))
            self._open_dm(other, other_id)
        self._split.set_show_content(True)

    def _on_dm_activated(self, lb, row):
        """Kept for compat — routes to unified handler."""
        self._on_chats_activated(lb, row)

    def _on_group_activated(self, lb, row):
        """Kept for compat — routes to unified handler."""
        self._on_chats_activated(lb, row)

    # ── Contacts (populated from group members) ──
    def _load_contacts(self):
        """Fetch group members in the background and populate the contacts tab."""
        self._contacts_spinner.set_spinning(True)
        self._contacts_spinner.set_visible(True)

        def worker():
            # Fetch groups WITH members (no omit=memberships)
            groups_with_members = self._api.get_groups_all_with_members()
            GLib.idle_add(self._populate_contacts_from_groups, groups_with_members)

        threading.Thread(target=worker, daemon=True).start()

    def _populate_contacts_from_groups(self, groups):
        """Build the contacts list from all group members across all groups."""
        me_id = str(self._current_user.get("id", "")) if self._current_user else ""

        seen: dict = {}   # user_id → contact dict
        for g in groups:
            for m in (g.get("members") or []):
                uid = str(m.get("user_id", ""))
                if not uid or uid == me_id:
                    continue
                name = m.get("nickname") or m.get("name") or uid
                if uid not in seen:
                    seen[uid] = {
                        "user_id"   : uid,
                        "name"      : name,
                        "avatar_url": m.get("image_url", ""),
                    }
                else:
                    # Prefer entries with an avatar
                    if not seen[uid].get("avatar_url") and m.get("image_url"):
                        seen[uid]["avatar_url"] = m["image_url"]

        contacts = sorted(seen.values(),
                          key=lambda c: (c.get("name") or "").lower())
        self._display_contacts(contacts)

    def _display_contacts(self, contacts):
        self._contacts_spinner.set_spinning(False)
        self._contacts_spinner.set_visible(False)
        self._all_contacts = contacts
        # Populate name cache from contact list
        for c in contacts:
            uid = str(c.get("user_id", ""))
            name = c.get("name", "")
            if uid and name:
                self._name_cache[uid] = name

        child = self._contacts_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._contacts_list.remove(child)
            child = nxt

        if not contacts:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            lbl = Gtk.Label(label="No contacts found in your groups")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(32); lbl.set_margin_bottom(32)
            row.set_child(lbl)
            self._contacts_list.append(row)
            return

        for c in contacts:
            self._contacts_list.append(ContactRow(c))

    # ── Selection handlers ──
    def _on_group_activated(self, lb, row):
        if not isinstance(row, GroupRow):
            return
        self._current_group = row.group
        row.set_unread(0)   # clears to dimmed "0" rather than hiding
        self._open_chat(row.group)
        self._split.set_show_content(True)

    def _on_contact_activated(self, lb, row):
        if not isinstance(row, ContactRow):
            return
        c        = row.contact
        other_id = str(c.get("user_id") or c.get("id") or "")
        self.open_dm_for_user(c, other_id)

    # ── Chat ──
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
        tw.append(tl)
        hdr.set_title_widget(tw)

        # Members button
        members_btn = Gtk.Button(icon_name="system-users-symbolic")
        members_btn.add_css_class("flat")
        members_btn.set_tooltip_text("Members")
        members_btn.connect("clicked", self._show_members_panel)
        hdr.pack_end(members_btn)

        # Group action menu
        grp_menu = Gio.Menu()
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

        self._content_nav.set_title(esc(group.get("name","Group")))
        self._content_wrap.append(hdr)

        # Register per-group window actions
        self._register_group_actions(group)

        cv = ChatView(
            self._api, group,
            self._current_user.get("id"),
            self,
            config=self._config,
        )
        cv.set_vexpand(True)
        self._content_wrap.append(cv)
        self._chat_view = cv

    # ── Group detail ──
    def _show_group_detail(self, *_):
        if not self._current_group:
            return

        def worker():
            g = self._api.get_group(self._current_group["id"])
            if g:
                GLib.idle_add(self._open_group_detail, g)

        threading.Thread(target=worker, daemon=True).start()

    def _open_group_detail(self, group):
        GroupDetailDialog(
            self._api, group,
            self._current_user.get("id"),
            self
        ).present(self)

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
        self._split.set_show_content(True)

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
        tw.append(tl)
        hdr.set_title_widget(tw)

        self._content_nav.set_title(esc(name))
        self._content_wrap.append(hdr)

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
        self._content_wrap.append(cv)
        self._chat_view = cv

    # ── Search ──
    def _on_search(self, entry):
        q = entry.get_text().lower()

        child = self._chats_list.get_first_child()
        while child:
            if isinstance(child, ConversationRow):
                conv = child.conv
                if child.conv_type == "group":
                    n = conv.get("name", "").lower()
                    d = conv.get("description", "").lower()
                    child.set_visible(not q or q in n or q in d)
                else:
                    n = (conv.get("other_user", {}).get("name", "")).lower()
                    p = (conv.get("last_message", {}).get("text", "") or "").lower()
                    child.set_visible(not q or q in n or q in p)
            child = child.get_next_sibling()

        child = self._contacts_list.get_first_child()
        while child:
            if isinstance(child, ContactRow):
                n = (child.contact.get("name") or "").lower()
                p = (child.contact.get("phone_number") or "").lower()
                e = (child.contact.get("email") or "").lower()
                child.set_visible(not q or q in n or q in p or q in e)
            child = child.get_next_sibling()

    # ── Actions ──
    # ── Background poll (all groups/DMs — for notifications & badges) ──
    BG_POLL_INTERVAL = 30_000   # ms — check every 30 s

    def _start_bg_poll(self):
        if self._bg_poll_id:
            GLib.source_remove(self._bg_poll_id)
        secs = int(self._config.get_pref("bg_poll_interval_secs", 30) or 30)
        interval_ms = max(10, secs) * 1000
        self._bg_poll_id = GLib.timeout_add(interval_ms, self._bg_poll)

    def _stop_bg_poll(self):
        if self._bg_poll_id:
            GLib.source_remove(self._bg_poll_id)
            self._bg_poll_id = None

    def _bg_poll(self):
        """Fetch all group/DM summaries to detect new messages."""
        if not self._api:
            return False

        def worker():
            groups = self._api.get_groups_all()
            chats  = self._api.get_chats_all()
            GLib.idle_add(self._process_bg_update, groups, chats)

        threading.Thread(target=worker, daemon=True).start()
        return True   # keep timer alive

    def _process_bg_update(self, groups, chats):
        current_gid = (str(self._current_group["id"])
                       if self._current_group else None)
        total_unread = 0

        # ── Groups ──
        for g in groups:
            gid      = str(g["id"])
            msgs     = g.get("messages", {})
            last_id  = msgs.get("last_message_id")
            unread   = int(msgs.get("unread_count") or 0)
            prev_id  = self._last_msg_ids.get(gid)
            total_unread += unread

            if last_id and last_id != prev_id:
                self._last_msg_ids[gid] = last_id

                # Move to top of unified chats list
                if gid in self._group_rows:
                    row = self._group_rows[gid]
                    self._chats_list.remove(row)
                    self._chats_list.insert(row, 0)
                    # Update time label
                    ts = msgs.get("last_message_created_at")
                    if ts:
                        row.update_time(ts)

                if gid != current_gid:
                    if gid in self._group_rows:
                        self._group_rows[gid].set_unread(unread)
                    if unread > 0:
                        preview = msgs.get("preview", {})
                        sender  = preview.get("nickname", "Someone")
                        text    = (preview.get("text") or "📎 attachment").strip()
                        name    = g.get("name", "GroupMe")
                        self._send_desktop_notification(
                            name, f"{sender}: {text}", tag=f"group-{gid}")
            elif gid in self._group_rows:
                self._group_rows[gid].set_unread(unread)

        # ── DMs ──
        for chat in chats:
            other_id = str(chat.get("other_user", {}).get("id", ""))
            key      = f"dm_{other_id}"
            last_id  = chat.get("last_message", {}).get("id")
            unread   = int(chat.get("unread_count") or 0)
            prev_id  = self._last_msg_ids.get(key)
            total_unread += unread

            is_open = (self._current_group is not None and
                       str(self._current_group.get("id", "")) == other_id)

            if last_id and last_id != prev_id:
                self._last_msg_ids[key] = last_id

                # Move to top of unified chats list
                if other_id in self._dm_rows:
                    row = self._dm_rows[other_id]
                    self._chats_list.remove(row)
                    self._chats_list.insert(row, 0)
                    ts = chat.get("last_message", {}).get("created_at")
                    if ts:
                        row.update_time(ts)

                if not is_open and unread > 0:
                    if other_id in self._dm_rows:
                        self._dm_rows[other_id].set_unread(unread)
                    other_name = chat.get("other_user", {}).get("name", "Someone")
                    last_text  = (chat.get("last_message", {})
                                      .get("text") or "📎 attachment").strip()
                    self._send_desktop_notification(
                        other_name, last_text, tag=f"dm-{other_id}")
            elif other_id in self._dm_rows:
                self._dm_rows[other_id].set_unread(unread)

        # Update tab attention dot
        if hasattr(self, '_chats_page'):
            self._chats_page.set_needs_attention(total_unread > 0)

    def _send_desktop_notification(self, title: str, body: str,
                                    tag: str = "banter-msg"):
        """Send a desktop notification via GApplication (works in Flatpak)."""
        if tag.startswith("group-"):
            gid = tag[len("group-"):]
            if self._config.is_muted(gid):
                dbg("notification suppressed (muted): %s", tag)
                return
        try:
            app = self.get_application()
            if app is None:
                return
            notif = Gio.Notification.new(title)
            notif.set_body(body[:200])
            # Use the app's own icon (registered in hicolor icon theme)
            notif.set_icon(Gio.ThemedIcon.new("land.rob.Banter"))
            notif.set_priority(Gio.NotificationPriority.HIGH)
            # Default action brings the window to the foreground on click
            notif.set_default_action("app.activate")
            app.send_notification(tag, notif)
            dbg("notification sent: [%s] %s – %s", tag, title, body[:60])
        except Exception as e:
            dbg("notification error: %s", e)

    def _register_group_actions(self, group: dict):
        """Register Gio.SimpleActions on the window for the current group menu."""
        win = self

        def _remove_action(name):
            try:
                win.remove_action(name)
            except Exception:
                pass

        for name in ("grp-album", "grp-events-view", "grp-event", "grp-poll", "grp-share", "grp-settings"):
            _remove_action(name)

        def _act(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            win.add_action(a)

        _act("grp-album",       lambda: GalleryDialog(self._api, group, self).present(self))
        me_id = (self._current_user or {}).get("id", "")
        _act("grp-events-view", lambda: EventsListDialog(self._api, group, me_id, self).present(self))
        _act("grp-event",       lambda: CreateEventDialog(self._api, group, self).present(self))
        _act("grp-poll",     lambda: CreatePollDialog(self._api, group, self).present(self))
        _act("grp-share",    lambda: self._share_group(group))
        _act("grp-settings", lambda: self._open_group_settings(group))

    def _share_group(self, group: dict):
        """Show share dialog with copy button, system share, and QR code."""
        share_url = group.get("share_url") or group.get("share_token")
        if not share_url:
            self.toast("This group has no share link")
            return

        dlg = Adw.Dialog()
        dlg.set_title("Share Group")
        dlg.set_content_width(360)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(20); box.set_margin_end(20)
        box.set_margin_top(16);   box.set_margin_bottom(20)

        # Group name
        name_lbl = Gtk.Label(label=esc(group.get("name", "Group")))
        name_lbl.add_css_class("title-2")
        box.append(name_lbl)

        # URL display
        url_row = Adw.ActionRow(title="Invite Link")
        url_row.set_subtitle(share_url)
        url_row.set_subtitle_selectable(True)
        box.append(url_row)

        # Copy button
        copy_btn = Gtk.Button(label="Copy Link")
        copy_btn.add_css_class("suggested-action")
        copy_btn.add_css_class("pill")
        copy_btn.set_icon_name("edit-copy-symbolic")

        def _copy(*_):
            Gdk.Display.get_default().get_clipboard().set(share_url)
            self.toast("Link copied!")
            dlg.close()

        copy_btn.connect("clicked", _copy)
        box.append(copy_btn)

        # System share button (via portal)
        share_btn = Gtk.Button(label="Share…")
        share_btn.add_css_class("pill")
        share_btn.set_icon_name("mail-forward-symbolic")

        def _system_share(*_):
            try:
                # Use Gio to trigger the system share sheet
                Gio.AppInfo.launch_default_for_uri(share_url, None)
            except Exception:
                # Fallback: just copy
                _copy()

        share_btn.connect("clicked", _system_share)
        box.append(share_btn)

        # QR code using pure Python (no external library)
        try:
            qr_widget = self._make_qr_widget(share_url)
            if qr_widget:
                sep = Gtk.Separator()
                sep.set_margin_top(4); sep.set_margin_bottom(4)
                box.append(sep)
                qr_lbl = Gtk.Label(label="Scan to join")
                qr_lbl.add_css_class("dim-label")
                box.append(qr_lbl)
                box.append(qr_widget)
        except Exception as e:
            dbg("QR generation failed: %s", e)

        tv.set_content(box)
        dlg.set_child(tv)
        dlg.present(self)

    def _make_qr_widget(self, url: str):
        """Generate a QR code as a DrawingArea using the qrcode library if available,
        or a cairo-drawn pixel grid if not."""
        try:
            import qrcode
            qr = qrcode.QRCode(border=2)
            qr.add_data(url)
            qr.make(fit=True)
            matrix = qr.get_matrix()
        except ImportError:
            # Minimal QR fallback: draw a placeholder with the URL text
            lbl = Gtk.Label(label="Install 'qrcode' package for QR codes\n(pip install qrcode)")
            lbl.add_css_class("dim-label")
            lbl.set_wrap(True)
            lbl.set_justify(Gtk.Justification.CENTER)
            return lbl

        n    = len(matrix)
        size = min(240, 240)
        cell = size // n

        area = Gtk.DrawingArea()
        area.set_size_request(n * cell, n * cell)
        area.set_halign(Gtk.Align.CENTER)

        def _draw(widget, cr, width, height):
            import cairo
            cr.set_source_rgb(1, 1, 1)
            cr.paint()
            cr.set_source_rgb(0, 0, 0)
            for r, row in enumerate(matrix):
                for c, dark in enumerate(row):
                    if dark:
                        cr.rectangle(c * cell, r * cell, cell, cell)
                        cr.fill()

        area.set_draw_func(_draw)
        return area

    def _open_group_settings(self, group: dict = None):
        g = group or self._current_group
        if not g:
            return

        def worker():
            full = self._api.get_group(g["id"])
            GLib.idle_add(lambda: GroupSettingsDialog(
                self._api, full or g,
                self._current_user.get("id"),
                self._config, self
            ).present(self))

        threading.Thread(target=worker, daemon=True).start()

    def _show_members_panel(self, *_):
        if not self._current_group:
            return

        def worker():
            full = self._api.get_group(self._current_group["id"])
            GLib.idle_add(lambda: MembersDialog(
                self._api, full or self._current_group,
                self._current_user.get("id"),
                self
            ).present(self))

        threading.Thread(target=worker, daemon=True).start()

    def _open_preferences(self, *_):
        PreferencesDialog(self._config, self, self._chat_view).present(self)

    def _setup_actions(self):
        app = self.get_application()

        so = Gio.SimpleAction.new("sign-out", None)
        so.connect("activate", self._sign_out)
        app.add_action(so)

        acc_act = Gio.SimpleAction.new("accounts", None)
        acc_act.connect("activate", self._manage_accounts)
        app.add_action(acc_act)

        pref_act = Gio.SimpleAction.new("preferences", None)
        pref_act.connect("activate", self._open_preferences)
        app.add_action(pref_act)

    def _sign_out(self, *_):
        self._stop_bg_poll()
        if self._current_user:
            self._config.remove_account(
                str(self._current_user.get("id","")))
        self._api          = None
        self._current_user = None
        self._last_msg_ids = {}
        self._group_rows   = {}
        self._dm_rows      = {}
        self._conv_rows    = {}
        self._stop_push()
        self._show_placeholder()
        self._go_login()

    def _manage_accounts(self, *_):
        def on_switch(acc):
            if acc is None:
                self._sign_out()
                return
            self._config.set_active_account(acc["user_id"])
            self._api = GroupMeAPI(acc["token"])

            def reload():
                me = self._api.get_me()
                if me:
                    GLib.idle_add(self._reload_for_user, me)

            threading.Thread(target=reload, daemon=True).start()

        AccountsDialog(self._config, self, on_switch).present(self)

    def _reload_for_user(self, user):
        self._current_user = user
        self._last_msg_ids = {}
        self._group_rows   = {}
        self._dm_rows      = {}
        self._conv_rows    = {}
        self._stop_push()
        self._show_placeholder()
        self.refresh_chats()
        self._load_contacts()
        self._start_bg_poll()
        self._start_push()


# ─────────────────────────── Application ─────────────────────────

