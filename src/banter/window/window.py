"""BanterWindow — the templated main window.

This file owns the things that have to live on the actual class:
the @Gtk.Template binding, the Gtk.Template.Child descriptor
declarations, the four @Gtk.Template.Callback callbacks (GTK looks
them up on the class), and the constructor that wires instance
state used by all the feature mixins.

The feature areas live in sibling mixin modules and are pulled in
via multiple inheritance — the public class is the assembly point.
"""

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from ..api import GroupMeAPI
from ..async_utils import run_in_background
from ..constants import APP_ID, APP_NAME, DEMO
from ..helpers import ensure_packs_loaded
from ..mock_api import MockGroupMeAPI
from ..notifications import NotificationDispatcher
from ._actions import ActionsMixin
from ._bg_poll import BackgroundPollMixin
from ._chat import ChatMixin
from ._login import LoginMixin
from ._mute import MuteMixin
from ._push import PushMixin
from ._sidebar import SidebarMixin

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/banter/ui/window.ui")
class BanterWindow(
    LoginMixin,
    PushMixin,
    SidebarMixin,
    ChatMixin,
    BackgroundPollMixin,
    ActionsMixin,
    MuteMixin,
    Adw.ApplicationWindow,
):
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

    # Don't show the banner immediately — DNS hiccups, brief Wi-Fi
    # roams, and similar transient flaps fail and recover in well
    # under 5 s. We delay the reveal so a flap that resolves before
    # the grace expires is invisible to the user.
    OFFLINE_GRACE_MS = 5_000

    def __init__(self, app):
        super().__init__(application=app)
        self._config       = app.config
        self._notifications = NotificationDispatcher(app, self._config)
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

        # Close-to-background: when the "Run in background" preference
        # is on we hide the window instead of quitting and call
        # app.hold() to keep the push + notification path alive. The
        # `_held` flag mirrors that ref-count so reactivation can
        # release exactly once.
        self._held = False
        self.connect("close-request", self._on_close_request)
        self.connect("show",          self._on_window_shown)

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

    # ── Connectivity banner ──
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

    # ── Name cache ──
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

    # ── Placeholder / content reset ──
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

    # ── App-level action wiring ──
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

    # ── Close-to-background ──
    def _on_close_request(self, *_):
        if not self._config.get_pref("close_to_background", False):
            return False
        if not self._config.get_active_account():
            return False
        # Hide and hold. The hidden window keeps its push client and
        # notification dispatcher running so messages still surface
        # via Gio.Notification; do_activate on the app re-presents
        # this same window when a notification (or relaunch) wakes it.
        self.set_visible(False)
        app = self.get_application()
        if app and not self._held:
            app.hold()
            self._held = True
            n = Gio.Notification.new("Banter is running in the background")
            n.set_body("You'll still get message notifications. "
                        "Click to reopen.")
            n.set_default_action("app.activate")
            app.send_notification("banter-bg-running", n)
        return True

    def _on_window_shown(self, *_):
        app = self.get_application()
        if app and self._held:
            app.release()
            self._held = False
        if app:
            app.withdraw_notification("banter-bg-running")
