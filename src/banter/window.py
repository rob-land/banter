"""Banter — BanterWindow: the application's primary window."""

import time
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, Pango

from .constants import APP_ID, APP_NAME, DEMO, dbg, esc
from .async_utils import run_in_background
from .config import Config
from .api import GroupMeAPI
from .push import GroupMePush
from .mock_api import MockGroupMeAPI
from .helpers import (
    set_avatar_from_url, ensure_packs_loaded, is_hidden_system_message,
    format_preview)
from .widgets.base import StandardDialog
from .widgets.conversation_row import ConversationRow, ContactRow
from .widgets.chat_view import ChatView
from .oauth import LoginDialog
from .dialogs.accounts import AccountsDialog
from .dialogs.group import NewGroupDialog, ContactDetailDialog
from .dialogs.members import MembersDialog
from .dialogs.settings import GroupSettingsDialog
from .dialogs.gallery import GalleryDialog
from .dialogs.events import CreateEventDialog, EventsListDialog, CreatePollDialog
from .dialogs.pinned import PinnedDialog
from .dialogs.profile import EditProfileDialog
from .dialogs.jump_to_date import JumpToDateDialog


@Gtk.Template(resource_path="/land/rob/Banter/ui/window.ui")
class BanterWindow(Adw.ApplicationWindow):
    __gtype_name__ = "BanterWindow"

    # Top-level shell
    toast_overlay:    Adw.ToastOverlay         = Gtk.Template.Child()
    main_box:         Gtk.Box                  = Gtk.Template.Child()
    offline_banner:   Adw.Banner               = Gtk.Template.Child()
    split:            Adw.NavigationSplitView  = Gtk.Template.Child()

    # Sidebar
    sidebar_nav:      Adw.NavigationPage       = Gtk.Template.Child()
    sidebar_tv:       Adw.ToolbarView          = Gtk.Template.Child()
    sidebar_header:   Adw.HeaderBar            = Gtk.Template.Child()
    accounts_button:  Gtk.MenuButton           = Gtk.Template.Child()
    new_group_button: Gtk.Button               = Gtk.Template.Child()
    top_switcher:     Adw.ViewSwitcher         = Gtk.Template.Child()
    bottom_switcher:  Adw.ViewSwitcherBar      = Gtk.Template.Child()
    search_entry:     Gtk.SearchEntry          = Gtk.Template.Child()
    stack:            Adw.ViewStack            = Gtk.Template.Child()
    chats_stack_page: Adw.ViewStackPage        = Gtk.Template.Child()
    chats_list:       Gtk.ListBox              = Gtk.Template.Child()
    chats_spinner:    Gtk.Spinner              = Gtk.Template.Child()
    contacts_list:    Gtk.ListBox              = Gtk.Template.Child()
    contacts_spinner: Gtk.Spinner              = Gtk.Template.Child()

    # Content (per-chat view is mounted into content_wrap by _open_chat)
    content_nav:      Adw.NavigationPage       = Gtk.Template.Child()
    content_tv:       Adw.ToolbarView          = Gtk.Template.Child()
    content_wrap:     Gtk.Box                  = Gtk.Template.Child()

    def __init__(self, app):
        super().__init__(application=app)
        self._config       = Config()
        self._api          = None
        self._current_user = None
        self._current_group= None
        self._chat_view    = None
        self._all_groups   : list = []
        # Same groups, but fetched WITH full member lists. Populated by
        # _load_contacts / _populate_contacts_from_groups. Used by the
        # contact-detail sheet to compute mutual groups.
        self._all_groups_with_members : list = []
        self._all_dms      : list = []
        self._all_contacts : list = []
        # uid → display name cache, built from contacts + message senders
        self._name_cache   : dict = {}
        # Unified chats list (groups + DMs merged, sorted by recency)
        self._all_chats    : list = []
        # Conversation rows + last-seen message ids, keyed by a uniform
        # (conv_type, id) tuple. Use _conv_key() to construct keys; never
        # mix string and tuple keys here.
        self._rows         : dict = {}   # tuple → ConversationRow
        self._last_msg_ids : dict = {}   # tuple → last message id seen
        # Per-conversation compose drafts. Keyed by _conv_key tuple. The
        # active ChatView writes its current entry text here when it
        # stops (chat switch / window close), and a new ChatView reads
        # the value from here on construction. In-memory only — drafts
        # don't persist across app restarts.
        self._drafts       : dict = {}
        # poll_id → list[PollCard] for currently-mounted poll widgets.
        # PollCards register on __init__; the list is cleared whenever a
        # new ChatView is built (the previous chat's cards die with it).
        self._poll_cards   : dict = {}
        self._bg_poll_id   = None
        self._push         = None   # singleton GroupMePush for the whole session
        # Pending GLib.timeout id for the debounced offline-banner
        # reveal. 0 means no reveal scheduled (so the next failure
        # arms one).
        self._offline_show_id = 0
        # Per-chat header bar built by _open_chat / _open_dm and mounted
        # into the templated content_tv. Tracked so _clear_content can
        # detach it before the next chat opens.
        self._content_header = None
        # Bell button on the per-chat header (set up in _open_chat /
        # _open_dm); _apply_mute reads this to refresh the icon and
        # menu after a mute change.
        self._mute_btn = None

        if DEMO:
            self._api = MockGroupMeAPI()
            self._enter_main(self._api.get_me())
            return

        acc = self._config.get_active_account()
        if acc:
            self._api = GroupMeAPI(acc["token"],
                                    on_unauthorized=self._on_session_expired,
                                    on_online=self._on_api_online,
                                    on_offline=self._on_api_offline)
            # Hide the main shell behind a verifying-spinner until the
            # token check resolves; the templated layout exists already
            # but we don't want users interacting with it before auth.
            spinner = Gtk.Spinner(spinning=True, margin_top=120,
                                   halign=Gtk.Align.CENTER)
            self.toast_overlay.set_child(spinner)

            def verify():
                me = self._api.get_me()
                if me:
                    GLib.idle_add(self._enter_main, me)
                else:
                    GLib.idle_add(self._go_login)

            run_in_background(verify)
        else:
            self._go_login()

    # ── Template callbacks (wired up in data/ui/window.blp) ──
    @Gtk.Template.Callback()
    def on_new_group_clicked(self, _btn):
        self._new_group()

    @Gtk.Template.Callback()
    def on_search_changed(self, entry):
        self._on_search(entry)

    @Gtk.Template.Callback()
    def on_chats_activated(self, lb, row):
        self._on_chats_activated(lb, row)

    @Gtk.Template.Callback()
    def on_contact_activated(self, lb, row):
        self._on_contact_activated(lb, row)

    # ── Mute helpers ──
    @staticmethod
    def _mute_key(conv_type: str, conv_id) -> str:
        """Config key for the mute store. Groups use their bare gid
        (matches the legacy v1 schema); DMs are prefixed `dm:` so the
        key spaces don't collide."""
        s = str(conv_id)
        return f"dm:{s}" if conv_type == "dm" else s

    def is_conv_muted(self, conv_type: str, conv_id) -> bool:
        return self._config.is_muted(self._mute_key(conv_type, conv_id))

    def _refresh_mute_button(self, btn, conv_type: str, conv_id):
        muted = self.is_conv_muted(conv_type, conv_id)
        # Adwaita only ships notifications-DISABLED-symbolic, so we
        # bundle our own bell-without-slash for the unmuted state
        # (see data/meson.build).
        btn.set_icon_name(
            "notifications-disabled-symbolic" if muted
            else "banter-notifications-active-symbolic")
        btn.set_tooltip_text(
            "Unmute notifications" if muted else "Mute notifications")

    # Timed-mute presets surfaced in the bell-button menu. Tuples of
    # (label, seconds) — seconds=-1 is "until I turn it back on" (the
    # config layer's permanent sentinel).
    _MUTE_PRESETS = (
        ("1 hour",                   3600),
        ("8 hours",                  8 * 3600),
        ("Until I turn it back on", -1),
    )

    @staticmethod
    def _mute_menu_item(label: str, secs: int) -> Gio.MenuItem:
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(
            "win.set-mute", GLib.Variant.new_int32(secs))
        return item

    def _build_mute_menu(self, conv_type: str, conv_id) -> Gio.Menu:
        """Build the bell-button menu. 'Unmute' appears in its own
        section above the durations only when the conv is currently
        muted, so users always see a meaningful first option."""
        menu = Gio.Menu()
        if self.is_conv_muted(conv_type, conv_id):
            sec = Gio.Menu()
            sec.append_item(self._mute_menu_item("Unmute", 0))
            menu.append_section(None, sec)
        durations = Gio.Menu()
        for label, secs in self._MUTE_PRESETS:
            durations.append_item(self._mute_menu_item(label, secs))
        menu.append_section(None, durations)
        return menu

    def _apply_mute(self, conv_type: str, conv_id, secs: int):
        """Apply a mute change picked from the bell menu. Updates
        config, the bell icon, the sidebar row, and the menu model
        (so 'Unmute' appears/disappears on the next open)."""
        key = self._mute_key(conv_type, conv_id)
        if secs == 0:
            self._config.clear_mute(key)
            msg = "Notifications on"
        elif secs == -1:
            self._config.set_mute(key, -1)
            msg = "Muted until you turn it back on"
        else:
            until = int(time.time()) + secs
            self._config.set_mute(key, until)
            msg = f"Muted for {next(
                (lbl for lbl, s in self._MUTE_PRESETS if s == secs),
                f'{secs} seconds')}"

        if self._mute_btn is not None:
            self._refresh_mute_button(self._mute_btn, conv_type, conv_id)
            self._mute_btn.set_menu_model(
                self._build_mute_menu(conv_type, conv_id))
        row = self._rows.get(self._conv_key(conv_type, conv_id))
        if row is not None and hasattr(row, "set_muted"):
            row.set_muted(self._config.is_muted(key))
        try:
            self.toast(msg)
        except Exception:
            pass

    # ── Call (Teams meeting) ───────────────────────────────────────
    def _on_call_clicked(self, _btn, conv_id):
        """Fetch the call session for this conversation and open the
        Teams meeting URL in the system browser. The official client
        embeds Azure Communication Services' Teams composite; from a
        Linux app without that SDK, the browser is the right place
        for the user to grant camera/mic and join the call."""
        self.toast("Starting call…")
        api = self._api

        def worker():
            r = api.get_call(conv_id)
            GLib.idle_add(self._open_call, r)

        run_in_background(worker)

    def _open_call(self, call: dict):
        if not call:
            self.toast("Couldn't start call")
            return
        url = call.get("meeting_id", "")
        if not url:
            self.toast("Couldn't start call")
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception as e:
            dbg("call launch failed: %s", e)
            self.toast("Couldn't open browser for call")

    # ── Conversation key helper ──
    @staticmethod
    def _conv_key(conv_type: str, conv_id) -> tuple:
        """Build the tuple key used by _rows and _last_msg_ids for a
        given conversation. conv_type is 'group' or 'dm'; conv_id is
        the group id (for groups) or the OTHER user's id (for DMs)."""
        return (conv_type, str(conv_id))

    # ── Toast helper ──
    def toast(self, msg: str):
        self.toast_overlay.add_toast(Adw.Toast.new(msg))

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
        self._api = GroupMeAPI(token,
                                on_unauthorized=self._on_session_expired,
                                on_online=self._on_api_online,
                                on_offline=self._on_api_offline)
        self._config.add_account(token, user)
        self._build_main_ui(user)

    def _on_session_expired(self):
        """Fired by GroupMeAPI when a token-bearing request returns 401.
        Runs on the worker thread that did the request — bounce to the
        main thread, then drop the dead account and re-prompt sign-in."""
        GLib.idle_add(self._handle_session_expired)

    # ── Connectivity banner ──
    # Don't show the banner immediately — DNS hiccups, brief Wi-Fi
    # roams, and similar transient flaps fail and recover in well
    # under 5 s. We delay the reveal so a flap that resolves before
    # the grace expires is invisible to the user.
    OFFLINE_GRACE_MS = 5_000

    def _on_api_online(self):
        """Fired (worker thread) on the offline → online API transition."""
        GLib.idle_add(self._handle_online)

    def _on_api_offline(self):
        """Fired (worker thread) on the online → offline API transition."""
        GLib.idle_add(self._handle_offline)

    def _handle_offline(self):
        banner = getattr(self, "_offline_banner", None)
        if banner is None:
            return False
        # Already pending or shown — nothing to do.
        if getattr(self, "_offline_show_id", 0):
            return False
        if banner.get_revealed():
            return False
        self._offline_show_id = GLib.timeout_add(
            self.OFFLINE_GRACE_MS, self._reveal_offline_banner)
        return False

    def _reveal_offline_banner(self):
        self._offline_show_id = 0
        banner = getattr(self, "_offline_banner", None)
        if banner is not None:
            banner.set_revealed(True)
        return False   # one-shot timer

    def _handle_online(self):
        # Cancel any pending reveal — the flap resolved before the
        # grace window expired, so the user never needs to know.
        show_id = getattr(self, "_offline_show_id", 0)
        if show_id:
            GLib.source_remove(show_id)
            self._offline_show_id = 0
        banner = getattr(self, "_offline_banner", None)
        if banner is not None:
            banner.set_revealed(False)
        return False

    def _handle_session_expired(self):
        try:
            self.toast("Session expired — please sign in again")
        except Exception:
            pass
        # Stop the push client so it doesn't keep retrying with the
        # dead token in the background.
        self._stop_push()
        # Drop the active account from config so the next launch (or
        # the upcoming sign-in flow) doesn't try the same dead token.
        acc = self._config.get_active_account()
        if acc:
            self._config.remove_account(acc["user_id"])
        # Tear down whatever main-UI state we built so the login
        # dialog is the only thing the user sees, and bounce there.
        self._chat_view = None
        self._api = None
        self._current_user = None
        self._go_login()
        return False   # drop the idle_add

    # ── Main UI ──
    def _enter_main(self, user: dict):
        """Wire the templated shell to the live API session.

        The full layout (split view, sidebar, content area, offline
        banner, breakpoint) is defined in data/ui/window.blp; this
        method only restores main_box as the toast_overlay child (it
        was swapped out for the verifying-spinner during auth) and
        kicks off the data loads."""
        self._current_user = user

        # Restore the templated main shell (it was replaced by the
        # verifying-spinner in __init__ for the has-account path).
        self.toast_overlay.set_child(self.main_box)

        # Hover surfaces who's signed in without a non-actionable menu row.
        if user.get("name"):
            self.accounts_button.set_tooltip_text(f"Signed in as {user['name']}")

        self._show_placeholder()

        self._setup_actions()
        self.refresh_chats()
        self._load_contacts()
        self._start_bg_poll()
        self._start_push()
        # Warm the emoji-pack catalog so pack reactions and pack emoji
        # attachments render without a visible delay when first seen.
        ensure_packs_loaded(self._api)

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

    # ── Application-level push client (singleton) ──
    def _start_push(self):
        """Create and start the shared push client for this session."""
        if DEMO:
            return
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
        dbg("BanterWindow: push client started for user %s", user_id)

    def _stop_push(self):
        if self._push:
            self._push.stop()
            self._push = None

    # ── Poll card registry (live-update wiring) ──
    def register_poll_card(self, poll_id, card):
        """Called by PollCard.__init__ so push poll.vote events can
        find the live widget. The list is cleared whenever a new
        ChatView is built, so we don't accumulate dead cards."""
        if not poll_id:
            return
        self._poll_cards.setdefault(str(poll_id), []).append(card)

    def _clear_poll_cards(self):
        self._poll_cards.clear()

    def _on_push_event(self, data: dict):
        """Route push events to the active ChatView (if any)."""
        ev_type = data.get("type", "")
        subject = data.get("subject", {})
        dbg("push event: type=%s", ev_type)

        # Live poll vote — push fires on every vote (own + others) and
        # carries the full poll snapshot under subject.poll.data. Hand
        # it to any mounted PollCards for the same poll_id.
        if ev_type == "poll.vote":
            poll_data = (subject.get("poll") or {}).get("data") or {}
            pid = str(poll_data.get("id", ""))
            for card in list(self._poll_cards.get(pid, ())):
                card.apply_push_update(poll_data)
            return

        # Delegate to the currently open ChatView
        if self._chat_view:
            self._chat_view._on_push_event(data)

        # Also keep the sidebar unread counts fresh on new messages.
        # `direct_message.create` is the DM-specific event name some
        # endpoints emit; we accept both that and a `line.create`
        # without a group_id (the DM form on /user/{uid}).
        if ev_type in ("line.create", "direct_message.create"):
            if is_hidden_system_message(subject):
                return

            gid = str(subject.get("group_id", ""))
            if gid:
                self._handle_group_push_message(gid, subject)
            else:
                self._handle_dm_push_message(subject)

    def _handle_group_push_message(self, gid: str, subject: dict):
        key = self._conv_key("group", gid)
        row = self._rows.get(key)
        if row is None:
            return
        sender = subject.get("name", "")
        # Run preview text through format_preview so the server's
        # voice-note downgrade warning is replaced with a concise
        # "🎤 Voice message" instead of dominating the sidebar.
        preview_text = format_preview(subject.get("text"),
                                      subject.get("attachments"))

        # Always move to top, update preview + time — keeps the
        # sidebar in most-recent order regardless of which chat
        # is currently open.
        self.chats_list.remove(row)
        self.chats_list.insert(row, 0)
        ts = subject.get("created_at")
        if ts:
            row.update_time(ts)
        row.update_preview(sender, preview_text)

        # Mirror the bg_poll's _last_msg_ids bookkeeping so the next
        # poll doesn't re-fire a notification for the same message.
        msg_id = str(subject.get("id", ""))
        if msg_id:
            self._last_msg_ids[key] = msg_id

        # Call-started/ended events get a richer notification with a
        # Join button (and we surface them even when the chat is open).
        if self._handle_call_event_notification("group", gid, subject):
            return

        if self._is_conv_open("group", gid):
            return
        row.bump_unread()
        name = (row.conv or {}).get("name", "GroupMe")
        self._send_desktop_notification(
            name, f"{sender}: {preview_text or '📎 attachment'}",
            tag=f"group-{gid}")

    def _handle_dm_push_message(self, subject: dict):
        """Real-time DM notification path. Without this, DMs only
        notified via the 30 s bg_poll because the push event lacks
        a group_id and the legacy handler skipped it."""
        sender_uid = str(subject.get("user_id", "") or
                          subject.get("sender_id", ""))
        me_id = str((self._current_user or {}).get("id", ""))
        if not sender_uid or sender_uid == me_id:
            return   # self-echo of an outgoing send

        # The other party in this DM, from my perspective, is whoever
        # the sender is (since they're the participant that isn't me).
        other_id = sender_uid
        key      = self._conv_key("dm", other_id)
        row      = self._rows.get(key)

        sender = subject.get("name") or "Someone"
        preview_text = format_preview(subject.get("text"),
                                      subject.get("attachments"))

        if row is not None:
            self.chats_list.remove(row)
            self.chats_list.insert(row, 0)
            ts = subject.get("created_at")
            if ts:
                row.update_time(ts)
            row.update_preview(sender, preview_text)

        # Mirror bg_poll bookkeeping so the next /chats poll doesn't
        # double-fire a notification for the same message.
        msg_id = str(subject.get("id", ""))
        if msg_id:
            self._last_msg_ids[key] = msg_id

        # Same call-event handling as groups — DMs may not actually
        # carry call events in practice (no HAR evidence), but the
        # event shape would be identical so the handler is a no-op
        # cost and gives us coverage if it ever fires.
        if self._handle_call_event_notification("dm", other_id, subject):
            return

        if self._is_conv_open("dm", other_id):
            return
        if row is not None:
            row.bump_unread()
        self._send_desktop_notification(
            sender, preview_text or "📎 attachment", tag=f"dm-{other_id}")

    # ── Placeholder ──
    def _show_placeholder(self):
        self._clear_content()

        # Always show a header bar so the window-close button and the
        # mobile back-navigation button exist before any group is opened.
        hdr = Adw.HeaderBar()
        hdr.add_css_class("flat")
        self.content_tv.add_top_bar(hdr)
        self._content_header = hdr

        page = Adw.StatusPage(
            icon_name=APP_ID,
            title="Welcome to Banter",
            description="Select a group from the sidebar to start chatting."
        )
        page.set_vexpand(True)
        self.content_wrap.append(page)
        self.content_nav.set_title(APP_NAME)

    def _clear_content(self):
        if self._chat_view:
            self._chat_view.stop()
            self._chat_view = None
        if self._content_header is not None:
            self.content_tv.remove(self._content_header)
            self._content_header = None
        self._mute_btn = None
        child = self.content_wrap.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.content_wrap.remove(child)
            child = nxt

    # ── Unified Chats (groups + DMs) ──
    def refresh_chats(self):
        self.chats_spinner.set_spinning(True)
        self.chats_spinner.set_visible(True)

        def worker():
            groups = self._api.get_groups_all()
            chats  = self._api.get_chats_all()
            GLib.idle_add(self._display_chats, groups, chats)

        run_in_background(worker)

    # Keep refresh_groups as an alias for code that calls it (e.g. after deleting a group)
    def refresh_groups(self):
        self.refresh_chats()

    def _conv_sort_key(self, conv: dict, conv_type: str) -> int:
        """Return a unix timestamp used to sort conversations newest-first."""
        if conv_type == "dm":
            ts = (conv.get("last_message", {}).get("created_at") or
                  conv.get("updated_at") or 0)
        else:
            ts = (conv.get("messages", {}).get("last_message_created_at") or
                  conv.get("updated_at") or
                  conv.get("created_at") or 0)
        try:
            return int(ts)
        except (TypeError, ValueError):
            return 0

    def _display_chats(self, groups, chats):
        self.chats_spinner.set_spinning(False)
        self.chats_spinner.set_visible(False)
        self._all_groups = groups
        self._all_dms    = chats
        self._rows       = {}

        # Register every group with the push client so typing pulses and
        # other /group/{gid}-only events are delivered. Picked up on the
        # next /meta/connect reconnect cycle.
        if self._push is not None:
            for g in groups:
                gid = str(g.get("id", ""))
                if gid:
                    self._push.subscribe_group(gid)

        # Clear existing rows
        child = self.chats_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.chats_list.remove(child)
            child = nxt

        # Build combined list sorted by most-recent message
        items = (
            [("group", g) for g in groups] +
            [("dm",    c) for c in chats]
        )
        items.sort(key=lambda x: self._conv_sort_key(x[1], x[0]), reverse=True)

        me_id = (self._current_user or {}).get("id")
        for conv_type, conv in items:
            row = ConversationRow(conv, conv_type, self._config, me_id=me_id)
            self.chats_list.append(row)

            if conv_type == "group":
                gid = str(conv["id"])
                self._rows[self._conv_key("group", gid)] = row
                self._last_msg_ids.setdefault(
                    self._conv_key("group", gid),
                    conv.get("messages", {}).get("last_message_id"))
            else:
                other_id = str(conv.get("other_user", {}).get("id", ""))
                if other_id:
                    self._rows[self._conv_key("dm", other_id)] = row
                    self._last_msg_ids.setdefault(
                        self._conv_key("dm", other_id),
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
            self.chats_stack_page.set_needs_attention(total_unread > 0)

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
        self.split.set_show_content(True)

    # ── Contacts (populated from group members) ──
    def _load_contacts(self):
        """Fetch group members in the background and populate the contacts tab."""
        self.contacts_spinner.set_spinning(True)
        self.contacts_spinner.set_visible(True)

        def worker():
            # Fetch groups WITH members (no omit=memberships)
            groups_with_members = self._api.get_groups_all_with_members()
            GLib.idle_add(self._populate_contacts_from_groups, groups_with_members)

        run_in_background(worker)

    def _populate_contacts_from_groups(self, groups):
        """Build the contacts list from all group members across all groups."""
        # Keep the full-member copy around — the contact detail sheet
        # needs it to compute mutual groups without a second fetch.
        self._all_groups_with_members = groups or []
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
        self.contacts_spinner.set_spinning(False)
        self.contacts_spinner.set_visible(False)
        self._all_contacts = contacts
        # Populate name cache from contact list
        for c in contacts:
            uid = str(c.get("user_id", ""))
            name = c.get("name", "")
            if uid and name:
                self._name_cache[uid] = name

        child = self.contacts_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.contacts_list.remove(child)
            child = nxt

        if not contacts:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            lbl = Gtk.Label(label="No contacts found in your groups")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(32); lbl.set_margin_bottom(32)
            row.set_child(lbl)
            self.contacts_list.append(row)
            return

        for c in contacts:
            self.contacts_list.append(ContactRow(c))

    # ── Selection handlers ──
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

    # ── Search ──
    def _on_search(self, entry):
        q = entry.get_text().lower()

        child = self.chats_list.get_first_child()
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

        child = self.contacts_list.get_first_child()
        while child:
            if isinstance(child, ContactRow):
                n = (child.contact.get("name") or "").lower()
                p = (child.contact.get("phone_number") or "").lower()
                e = (child.contact.get("email") or "").lower()
                child.set_visible(not q or q in n or q in p or q in e)
            child = child.get_next_sibling()

    # ── Background poll (all groups/DMs — for notifications & badges) ──
    BG_POLL_INTERVAL_MS = 30_000

    def _start_bg_poll(self):
        if DEMO:
            return
        if self._bg_poll_id:
            GLib.source_remove(self._bg_poll_id)
        self._bg_poll_id = GLib.timeout_add(
            self.BG_POLL_INTERVAL_MS, self._bg_poll)

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

        run_in_background(worker)
        return True   # keep timer alive

    def _is_conv_open(self, conv_type: str, conv_id) -> bool:
        """Whether the active ChatView is showing this conversation.
        Uses ChatView's own state rather than `_current_group`, which
        is only ever set for groups (not DMs)."""
        cv = self._chat_view
        if cv is None:
            return False
        cid = str(conv_id)
        if conv_type == "dm":
            return bool(getattr(cv, "_is_dm", False)
                        and str(getattr(cv, "_other_uid", "")) == cid)
        return bool(not getattr(cv, "_is_dm", False)
                    and str(getattr(cv, "_gid", "")) == cid)

    def _process_bg_update(self, groups, chats):
        # The /groups and /chats responses both carry an `unread_count`
        # field, but in practice the value is `None` (not an integer)
        # for the vast majority of conversations — relying on it as
        # the notification gate caused DM notifications to never fire
        # at all. Instead we use "last_message_id changed since the
        # previous poll" as the new-message signal, plus a self-echo
        # filter so I don't get a notification for messages I just
        # sent. The local count of unread badges is also derived from
        # the same signal — there's no exact unread count without
        # fetching messages, so we just toggle a sidebar dot.
        me_id   = str((self._current_user or {}).get("id", ""))
        my_name = (self._current_user or {}).get("name", "")
        any_unread = False

        # ── Groups ──
        for g in groups:
            gid      = str(g["id"])
            key      = self._conv_key("group", gid)
            row      = self._rows.get(key)
            msgs     = g.get("messages", {})
            last_id  = msgs.get("last_message_id")
            prev_id  = self._last_msg_ids.get(key)

            if not (last_id and last_id != prev_id):
                continue
            self._last_msg_ids[key] = last_id

            # Move to top of unified chats list + refresh preview/time
            preview = msgs.get("preview", {}) or {}
            preview_text = format_preview(preview.get("text"),
                                          preview.get("attachments"))
            if row is not None:
                self.chats_list.remove(row)
                self.chats_list.insert(row, 0)
                ts = msgs.get("last_message_created_at")
                if ts:
                    row.update_time(ts)
                row.update_preview(preview.get("nickname", ""), preview_text)

            # Self-echo filter: group preview has nickname but no
            # user_id, so we fall back to comparing against our own
            # display name. Imperfect (a member with the same name
            # would be filtered too) but the failure mode is "missed
            # notification on a name collision" which is benign.
            sender = preview.get("nickname", "")
            from_me = bool(my_name) and sender == my_name
            if from_me or self._is_conv_open("group", gid):
                continue
            any_unread = True
            if row is not None:
                row.bump_unread()
            notif_text = preview_text or "📎 attachment"
            self._send_desktop_notification(
                g.get("name", "GroupMe"),
                f"{sender}: {notif_text}" if sender else notif_text,
                tag=f"group-{gid}")

        # ── DMs ──
        dbg("bg_poll: %d chats received", len(chats))
        for chat in chats:
            other_id = str(chat.get("other_user", {}).get("id", ""))
            key      = self._conv_key("dm", other_id)
            row      = self._rows.get(key)
            lm       = chat.get("last_message", {}) or {}
            last_id  = lm.get("id")
            prev_id  = self._last_msg_ids.get(key)

            if not last_id:
                dbg("bg_poll: dm %s has no last_message.id, skipping", other_id)
                continue
            if last_id == prev_id:
                continue
            dbg("bg_poll: dm %s NEW last_id=%s (prev=%s)",
                other_id, last_id, prev_id)
            self._last_msg_ids[key] = last_id

            # Move to top + refresh preview/time
            preview_text = format_preview(lm.get("text"), lm.get("attachments"))
            if row is not None:
                self.chats_list.remove(row)
                self.chats_list.insert(row, 0)
                ts = lm.get("created_at")
                if ts:
                    row.update_time(ts)
                sender_id = str(lm.get("sender_id") or lm.get("user_id") or "")
                if me_id and sender_id == me_id:
                    sender_for_preview = "You"
                else:
                    sender_for_preview = chat.get("other_user", {}).get("name", "")
                row.update_preview(sender_for_preview, preview_text)

            # Self-echo filter: DM last_message has user_id, so this
            # is exact (unlike groups).
            sender_id = str(lm.get("sender_id") or lm.get("user_id") or "")
            from_me   = bool(me_id) and sender_id == me_id
            is_open   = self._is_conv_open("dm", other_id)
            dbg("bg_poll: dm %s sender=%s me=%s from_me=%s open=%s",
                other_id, sender_id, me_id, from_me, is_open)
            if from_me or is_open:
                continue
            any_unread = True
            if row is not None:
                row.bump_unread()
            other_name = chat.get("other_user", {}).get("name", "Someone")
            self._send_desktop_notification(
                other_name, preview_text or "📎 attachment",
                tag=f"dm-{other_id}")

        # Sync each row's badge to the server's authoritative unread
        # count. Push events drive `bump_unread()` for instant feedback;
        # this pass corrects drift from other clients (a sibling phone
        # opening the chat clears unread there, and we want our count
        # to follow). Skipped for the currently-open conv — opening a
        # chat fires a read_receipt but the server may not have
        # processed it by the time this poll's response was built, so
        # we'd briefly re-display a stale count. The chat-open flow
        # already zeroes the row.
        for g in groups:
            gid = str(g["id"])
            row = self._rows.get(self._conv_key("group", gid))
            if row is None or self._is_conv_open("group", gid):
                continue
            sc = (g.get("messages") or {}).get("unread_count")
            if sc is not None:
                row.set_unread(int(sc))
        for chat in chats:
            other_id = str(chat.get("other_user", {}).get("id", ""))
            row = self._rows.get(self._conv_key("dm", other_id))
            if row is None or self._is_conv_open("dm", other_id):
                continue
            sc = chat.get("unread_count")
            if sc is not None:
                row.set_unread(int(sc))

        # Tab attention dot reflects persistent unread state, not just
        # "any new this poll" — derive from the per-row counters that
        # bump_unread / set_unread maintain.
        any_unread_persistent = any(
            getattr(r, "_unread_count_n", 0) > 0
            for r in self._rows.values())
        if hasattr(self, '_chats_page'):
            self.chats_stack_page.set_needs_attention(any_unread_persistent)

    def _send_desktop_notification(self, title: str, body: str,
                                    tag: str = "banter-msg",
                                    buttons: list = None,
                                    mute_key: str = None):
        """Send a desktop notification via GApplication (works in Flatpak).

        `buttons` is an optional list of (label, detailed_action) pairs
        passed straight to `Gio.Notification.add_button` — used by the
        call-started notification to attach a "Join" action.

        `mute_key` lets the caller specify the mute lookup key directly
        when the tag doesn't follow the `group-` / `dm-` convention
        (e.g. call notifications use `call-group-<gid>` to avoid being
        clobbered by an unrelated group message). When omitted we fall
        back to deriving the key from the tag prefix."""
        # Per-conversation mute. Tag layout is "group-{gid}" or
        # "dm-{other_id}"; the config store is unified, with DM keys
        # prefixed "dm:" so they don't collide with group ids.
        if mute_key is None:
            if tag.startswith("group-"):
                mute_key = tag[len("group-"):]
            elif tag.startswith("dm-"):
                mute_key = "dm:" + tag[len("dm-"):]
        if mute_key and self._config.is_muted(mute_key):
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
            for label, detailed in (buttons or ()):
                notif.add_button(label, detailed)
            app.send_notification(tag, notif)
            dbg("notification sent: [%s] %s – %s", tag, title, body[:60])
        except Exception as e:
            dbg("notification error: %s", e)

    def _withdraw_notification(self, tag: str):
        try:
            app = self.get_application()
            if app:
                app.withdraw_notification(tag)
        except Exception as e:
            dbg("notification withdraw failed (%s): %s", tag, e)

    def _handle_call_event_notification(self, conv_type: str,
                                        conv_id: str, subject: dict) -> bool:
        """Fire a call-specific notification with a Join button when a
        Faye `group.call.started` system message arrives, or withdraw
        the prior start notification on `group.call.ended`. Returns
        True iff the subject was a call event and was handled — caller
        should skip its generic notification path in that case.

        Unlike regular messages we surface this even when the chat is
        open, since the Join action is actionable from the OS notification
        layer too (and a call is a higher-signal event than a text)."""
        event = subject.get("event") or {}
        et = event.get("type", "")
        tag = f"call-{conv_type}-{conv_id}"
        mute_key = (conv_id if conv_type == "group" else f"dm:{conv_id}")

        if et == "group.call.ended":
            # Last participant left; clear the start notification.
            self._withdraw_notification(tag)
            return True

        if et != "group.call.started":
            return False

        data = event.get("data") or {}
        starter = ((data.get("user") or {}).get("nickname")
                   or "Someone")
        # Conversation display name for the title
        row = self._rows.get(self._conv_key(conv_type, conv_id))
        if row and conv_type == "group":
            title = (row.conv or {}).get("name") or "Group call"
        elif row and conv_type == "dm":
            other = (row.conv or {}).get("other_user") or {}
            title = other.get("name") or "Call"
        else:
            title = "GroupMe call"

        body = f"📞 {starter} started a call"
        # Detailed-action form passes the conv_id as the string param.
        # Group ids and DM <lo>+<hi> conv ids are both safe in this
        # encoding (digits/+/-).
        join_action = f"app.call-join::{conv_id}"
        self._send_desktop_notification(
            title, body, tag=tag,
            buttons=[("Join", join_action)],
            mute_key=mute_key)
        return True

    def _register_group_actions(self, group: dict):
        """Register Gio.SimpleActions on the window for the current group menu."""
        win = self

        def _remove_action(name):
            try:
                win.remove_action(name)
            except Exception:
                pass

        for name in ("set-mute", "grp-members", "grp-pinned", "grp-jump-date", "grp-album", "grp-events-view", "grp-event", "grp-poll", "grp-share", "grp-settings"):
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
            try: win.remove_action(name)
            except Exception: pass

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

    def _share_group(self, group: dict):
        """Show share dialog with copy button, system share, and QR code."""
        share_url = group.get("share_url") or group.get("share_token")
        if not share_url:
            self.toast("This group has no share link")
            return

        dlg = StandardDialog(title="Share Group", width=360, height=-1)

        def _copy(*_):
            Gdk.Display.get_default().get_clipboard().set(share_url)
            self.toast("Link copied!")
            dlg.close()

        copy_btn = Gtk.Button(label="Copy")
        copy_btn.add_css_class("suggested-action")
        copy_btn.connect("clicked", _copy)
        dlg.add_header_widget(copy_btn, end=True)

        box = dlg.set_scrolled_body(margin=20, spacing=16)

        # Group name
        name_lbl = Gtk.Label(label=esc(group.get("name", "Group")))
        name_lbl.add_css_class("title-2")
        box.append(name_lbl)

        # URL display
        url_row = Adw.ActionRow(title="Invite Link")
        url_row.set_subtitle(share_url)
        url_row.set_subtitle_selectable(True)
        box.append(url_row)

        # System share button (via portal)
        share_btn = Gtk.Button(label="Share…")
        share_btn.add_css_class("pill")
        share_btn.set_icon_name("mail-forward-symbolic")

        def _system_share(*_):
            try:
                # Use Gio to trigger the system share sheet
                Gio.AppInfo.launch_default_for_uri(share_url, None)
            except Exception as e:
                # Don't silently fall back to copy — the user clicked
                # Share, not Copy. Surface the failure and let them
                # retry or use the explicit Copy button.
                dbg("system share failed: %s", e)
                self.toast("Couldn't open share sheet — try Copy instead")

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

        run_in_background(worker)

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

        run_in_background(worker)

    def _setup_actions(self):
        app = self.get_application()

        so = Gio.SimpleAction.new("sign-out", None)
        so.connect("activate", self._sign_out)
        app.add_action(so)

        acc_act = Gio.SimpleAction.new("accounts", None)
        acc_act.connect("activate", self._manage_accounts)
        app.add_action(acc_act)

        # Ctrl+F: toggle the active chat view's search bar.
        find_act = Gio.SimpleAction.new("find", None)
        find_act.connect("activate", self._on_find)
        self.add_action(find_act)
        app.set_accels_for_action("win.find", ["<Control>f"])

        # Mark every conv with unread > 0 as read in one shot.
        mar_act = Gio.SimpleAction.new("mark-all-read", None)
        mar_act.connect("activate", self._on_mark_all_read)
        self.add_action(mar_act)

        # Edit My Profile dialog.
        ep_act = Gio.SimpleAction.new("edit-profile", None)
        ep_act.connect("activate", self._on_edit_profile)
        self.add_action(ep_act)

    def _on_find(self, *_):
        if self._chat_view is not None:
            self._chat_view.toggle_search()

    def _on_edit_profile(self, *_):
        if self._api is None:
            return
        EditProfileDialog(
            self._api, self,
            on_saved=self._on_profile_saved).present(self)

    def _on_profile_saved(self, user: dict):
        """After a successful profile save, refresh `_current_user`
        so name/avatar changes propagate to anywhere that reads it
        (account-button tooltip, the my_name self-echo filter, etc.).
        Existing UI rendered before this point isn't repainted —
        most things just rebuild on next chat-open or sidebar refresh,
        which is acceptable for a self-edit."""
        if isinstance(user, dict):
            self._current_user = user

    def _on_mark_all_read(self, *_):
        """Iterate every row with unread > 0 and POST a per-conversation
        read_receipt for it. We optimistically zero the badge first
        (good UX even if a request fails — the next bg_poll will
        correct any rows whose POST failed) and fan the requests out
        in a single worker thread."""
        if self._api is None:
            return
        targets: list = []   # list of (cid, row)
        for key, row in self._rows.items():
            if getattr(row, "_unread_count_n", 0) <= 0:
                continue
            conv_type, conv_id = key
            if conv_type == "group":
                cid = str(conv_id)
            else:
                # DM — same <lo>+<hi> conversation_id used by the
                # other receipt path.
                me = str((self._current_user or {}).get("id", ""))
                try:
                    a, b = int(me), int(conv_id)
                    lo, hi = (a, b) if a < b else (b, a)
                    cid = f"{lo}+{hi}"
                except (TypeError, ValueError):
                    cid = f"{me}+{conv_id}"
            targets.append((cid, row))

        if not targets:
            self.toast("No unread conversations")
            return

        # Optimistically zero each badge — server-side ack will land
        # within a tick, and the next bg_poll syncs anyway.
        for _cid, row in targets:
            try: row.set_unread(0)
            except Exception: pass

        api = self._api
        cids = [cid for cid, _ in targets]

        def worker():
            for cid in cids:
                try:
                    api.read_receipt(cid)
                except Exception as e:
                    dbg("mark-all-read: receipt for %s failed: %s", cid, e)

        run_in_background(worker)
        self.toast(f"Marked {len(targets)} read")

    def _sign_out(self, *_):
        self._stop_bg_poll()
        if self._current_user:
            self._config.remove_account(
                str(self._current_user.get("id","")))
        self._api          = None
        self._current_user = None
        self._last_msg_ids = {}
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

            run_in_background(reload)

        AccountsDialog(self._config, self, on_switch).present(self)

    def _reload_for_user(self, user):
        self._current_user = user
        self._last_msg_ids = {}
        self._stop_push()
        self._show_placeholder()
        self.refresh_chats()
        self._load_contacts()
        self._start_bg_poll()
        self._start_push()


# ─────────────────────────── Application ─────────────────────────

