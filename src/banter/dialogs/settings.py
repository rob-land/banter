"""Banter — GroupSettingsDialog."""

import time

from gi.repository import Adw, Gdk, GLib, Gtk

from ..async_utils import run_in_background


class GroupSettingsDialog(Adw.PreferencesDialog):
    MUTE_OPTIONS = [
        ("1 hour",     3600),
        ("8 hours",    28800),
        ("24 hours",   86400),
        ("1 week",     604800),
        ("Permanently", -1),
        ("Unmute",      0),
    ]

    def __init__(self, api, group, me_id, config, parent):
        super().__init__()
        self.set_title("Group Settings")
        self._api    = api
        self._group  = group
        self._me     = str(me_id)
        self._config = config
        self._parent = parent
        creator      = str(group.get("creator_user_id",""))
        self._is_owner = (creator == self._me)
        gid          = str(group["id"])

        page = Adw.PreferencesPage()
        self.add(page)

        # ── Group info edit (owner only) ──
        if self._is_owner:
            info_grp = Adw.PreferencesGroup(title="Group Info")
            self._name_row = Adw.EntryRow(title="Name")
            self._name_row.set_text(group.get("name",""))
            info_grp.add(self._name_row)
            self._desc_row = Adw.EntryRow(title="Description")
            self._desc_row.set_text(group.get("description","") or "")
            info_grp.add(self._desc_row)
            save_row = Adw.ButtonRow(title="Save Changes",
                                      start_icon_name="document-save-symbolic")
            save_row.add_css_class("suggested-action")
            save_row.connect("activated", self._save_info)
            info_grp.add(save_row)
            page.add(info_grp)

        # ── Invite link ──
        share_grp = Adw.PreferencesGroup(title="Sharing")
        share_url = group.get("share_url","")
        invite_row = Adw.ActionRow(title="Copy Invite Link",
                                    subtitle=share_url or "No link available")
        invite_row.set_activatable(True)
        invite_row.add_suffix(Gtk.Image.new_from_icon_name("edit-copy-symbolic"))
        invite_row.connect("activated", self._copy_invite)
        share_grp.add(invite_row)
        page.add(share_grp)

        # ── Notifications / mute ──
        notif_grp = Adw.PreferencesGroup(title="Notifications")
        currently_muted = config.is_muted(gid)
        mute_status = Adw.ActionRow(
            title="Status",
            subtitle="Muted" if currently_muted else "Receiving notifications")
        mute_status.add_suffix(
            Gtk.Image.new_from_icon_name(
                "notifications-disabled-symbolic" if currently_muted
                else "star-new-symbolic"))
        notif_grp.add(mute_status)

        for label, secs in self.MUTE_OPTIONS:
            row = Adw.ActionRow(title=label)
            row.set_activatable(True)
            if secs == 0 and currently_muted:
                row.add_css_class("success")
            row.connect("activated", self._set_mute, gid, secs)
            notif_grp.add(row)
        page.add(notif_grp)

        # ── Admin actions ──
        admin_grp = Adw.PreferencesGroup(title="Administration")
        if self._is_owner:
            del_row = Adw.ButtonRow(title="Delete Group",
                                     start_icon_name="user-trash-symbolic")
            del_row.add_css_class("destructive-action")
            del_row.connect("activated", self._delete_group)
            admin_grp.add(del_row)
        else:
            leave_row = Adw.ButtonRow(title="Leave Group",
                                       start_icon_name="system-log-out-symbolic")
            leave_row.add_css_class("destructive-action")
            leave_row.connect("activated", self._leave_group)
            admin_grp.add(leave_row)
        page.add(admin_grp)

    def _save_info(self, *_):
        name = self._name_row.get_text().strip()
        desc = self._desc_row.get_text().strip()
        def worker():
            r = self._api.update_group(self._group["id"], name=name, description=desc)
            GLib.idle_add(lambda: self._parent.toast(
                "Saved" if r else "Save failed"))
        run_in_background(worker)

    def _copy_invite(self, *_):
        url = self._group.get("share_url","")
        if url:
            Gdk.Display.get_default().get_clipboard().set(url)
            self._parent.toast("Invite link copied!")

    def _set_mute(self, row, gid: str, secs: int):
        if secs == 0:
            self._config.clear_mute(gid)
            self._parent.toast("Notifications unmuted")
        elif secs == -1:
            self._config.set_mute(gid, -1)
            self._parent.toast("Notifications muted permanently")
        else:
            until = time.time() + secs
            self._config.set_mute(gid, until)
            hrs = secs // 3600
            label = f"{hrs}h" if hrs else f"{secs//60}m"
            self._parent.toast(f"Muted for {label}")
        self.close()

    def _delete_group(self, *_):
        dlg = Adw.AlertDialog(
            heading="Delete Group?",
            body=f"'{self._group.get('name')}' will be permanently deleted.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", self._on_delete_resp)
        dlg.present(self._parent)

    def _on_delete_resp(self, dlg, resp):
        if resp != "delete":
            return
        def worker():
            ok = self._api.destroy_group(self._group["id"])
            GLib.idle_add(self._after_delete, ok)
        run_in_background(worker)

    def _after_delete(self, ok):
        self._parent.toast("Group deleted" if ok else "Delete failed")
        if ok:
            self.close()
            self._parent.refresh_groups()

    def _leave_group(self, *_):
        mid = next((m.get("id") for m in self._group.get("members",[])
                    if str(m.get("user_id","")) == self._me), None)
        if not mid:
            return
        def worker():
            ok = self._api.remove_member(self._group["id"], mid)
            GLib.idle_add(lambda: (
                self._parent.toast("Left group" if ok else "Failed"),
                ok and self.close(),
                ok and self._parent.refresh_groups()
            ))
        run_in_background(worker)
