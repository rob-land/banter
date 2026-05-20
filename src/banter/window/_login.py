"""LoginMixin — sign-in, sign-out, account switch, profile editing.

Mixed into BanterWindow. Owns the `GroupMeAPI` lifecycle: builds
the client on a fresh login or account switch (always wiring the
session/online/offline callbacks via `self._on_session_expired` /
`self._on_api_online` / `self._on_api_offline` so the connectivity
banner stays accurate after the swap).
"""

from gi.repository import GLib

from ..api import GroupMeAPI
from ..async_utils import run_in_background
from ..dialogs.accounts import AccountsDialog
from ..dialogs.profile import EditProfileDialog
from ..oauth import LoginDialog


class LoginMixin:
    def _go_login(self):
        self._login_dialog = LoginDialog(self, on_login=self._on_login)
        self._login_dialog.present(self)

    def _on_login(self, token: str, user: dict):
        self._login_dialog = None
        self._api = GroupMeAPI(token,
                                on_unauthorized=self._on_session_expired,
                                on_online=self._on_api_online,
                                on_offline=self._on_api_offline)
        self._config.add_account(token, user)
        self._enter_main(user)

    def _on_session_expired(self):
        """Fired by GroupMeAPI when a token-bearing request returns 401.
        Runs on the worker thread that did the request — bounce to the
        main thread, then drop the dead account and re-prompt sign-in."""
        GLib.idle_add(self._handle_session_expired)

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
            self._api = GroupMeAPI(acc["token"],
                                    on_unauthorized=self._on_session_expired,
                                    on_online=self._on_api_online,
                                    on_offline=self._on_api_offline)

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
