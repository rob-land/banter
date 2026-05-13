"""Banter — AccountsDialog (multi-account switcher)."""

from gi.repository import Gtk, Adw

from ..constants import esc
from ..config import Config
from ..helpers import set_avatar_from_url


class AccountsDialog(Adw.PreferencesDialog):
    def __init__(self, config: Config, parent, on_switch):
        super().__init__()
        self.set_title("Accounts")
        self._config    = config
        self._parent    = parent
        self._on_switch = on_switch

        page = Adw.PreferencesPage()
        self.add(page)

        self._accs_grp = Adw.PreferencesGroup(title="Signed-in Accounts")
        page.add(self._accs_grp)

        # Trailing "Add Account" row — the GNOME Settings pattern for
        # "add to a list" is a row at the end of the same group rather
        # than a free-floating pill button.
        self._add_row = Adw.ButtonRow(title="Add Account",
                                       start_icon_name="list-add-symbolic")
        self._add_row.add_css_class("suggested-action")
        self._add_row.connect("activated", self._add_account)

        # Track rows we own so we can clear them on refresh — walking
        # AdwPreferencesGroup with get_first_child() hits internal
        # widgets that aren't valid remove() targets.
        self._account_rows = []
        self._add_row_attached = False
        self._refresh_list()

    def _refresh_list(self):
        for row in self._account_rows:
            self._accs_grp.remove(row)
        self._account_rows = []
        if self._add_row_attached:
            self._accs_grp.remove(self._add_row)
            self._add_row_attached = False

        active = self._config.get_active_account()
        active_id = active["user_id"] if active else None

        for acc in self._config.get_accounts():
            row = Adw.ActionRow(title=esc(acc.get("name", "Account")),
                                 subtitle=esc(acc.get("email","") or
                                           acc.get("phone_number","")))
            av = Adw.Avatar(size=40, text=esc(acc.get("name","A")),
                             show_initials=True)
            set_avatar_from_url(av, acc.get("avatar_url",""))
            row.add_prefix(av)

            if acc["user_id"] == active_id:
                ck = Gtk.Image.new_from_icon_name("object-select-symbolic")
                row.add_suffix(ck)
            else:
                sw_btn = Gtk.Button(label="Switch")
                sw_btn.add_css_class("flat")
                sw_btn.set_valign(Gtk.Align.CENTER)
                sw_btn.connect("clicked", self._switch, acc)
                row.add_suffix(sw_btn)

            rm_btn = Gtk.Button(icon_name="list-remove-symbolic")
            rm_btn.add_css_class("flat")
            rm_btn.add_css_class("destructive-action")
            rm_btn.set_valign(Gtk.Align.CENTER)
            rm_btn.connect("clicked", self._remove, acc)
            row.add_suffix(rm_btn)

            self._accs_grp.add(row)
            self._account_rows.append(row)

        # Add Account row always lives at the bottom of the group.
        self._accs_grp.add(self._add_row)
        self._add_row_attached = True

    def _switch(self, btn, acc):
        self._config.set_active_account(acc["user_id"])
        self._on_switch(acc)
        self._refresh_list()

    def _remove(self, btn, acc):
        self._config.remove_account(acc["user_id"])
        self._refresh_list()
        remaining = self._config.get_accounts()
        if not remaining:
            self.close()
            self._on_switch(None)
        elif self._config.get_active_account() is None and remaining:
            self._on_switch(remaining[0])

    def _add_account(self, *_):
        from ..oauth import LoginDialog
        dlg = LoginDialog(self._parent,
                          on_login=self._on_new_account,
                          quit_on_cancel=False)
        dlg.present(self._parent)

    def _on_new_account(self, token, user):
        self._config.add_account(token, user)
        self._on_switch(self._config.get_active_account())
        self._refresh_list()
