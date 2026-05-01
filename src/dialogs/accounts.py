"""Banter — AccountsDialog (multi-account switcher)."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib

from ..constants import esc
from ..config import Config
from ..helpers import set_avatar_from_url
from ..widgets.base import StandardDialog


class AccountsDialog(StandardDialog):
    def __init__(self, config: Config, parent, on_switch):
        super().__init__(title="Accounts", width=400, height=420)
        self._config    = config
        self._parent    = parent
        self._on_switch = on_switch

        body = self.set_scrolled_body(margin=12, spacing=12)

        self._accs_grp = Adw.PreferencesGroup(title="Signed-in Accounts")
        body.append(self._accs_grp)

        add_btn = Gtk.Button(label="Add Account")
        add_btn.add_css_class("suggested-action")
        add_btn.add_css_class("pill")
        add_btn.connect("clicked", self._add_account)
        body.append(add_btn)

        self._refresh_list()

    def _refresh_list(self):
        child = self._accs_grp.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            try:
                self._accs_grp.remove(child)
            except Exception:
                pass
            child = nxt

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
        dlg = LoginDialog(self._parent,
                           on_login=self._on_new_account)
        dlg.present(self._parent)

    def _on_new_account(self, token, user):
        self._config.add_account(token, user)
        self._on_switch(self._config.get_active_account())
        self._refresh_list()


# ─────────────────────────── OAuth Callback Server ───────────────
