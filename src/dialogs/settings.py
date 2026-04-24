"""Banter — GroupSettingsDialog and PreferencesDialog."""

import threading
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio

from ..constants import dbg, esc


class GroupSettingsDialog(Adw.Dialog):
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
        self._api    = api
        self._group  = group
        self._me     = str(me_id)
        self._config = config
        self._parent = parent
        creator      = str(group.get("creator_user_id",""))
        self._is_owner = (creator == self._me)
        gid          = str(group["id"])

        self.set_title("Group Settings")
        self.set_content_width(420)
        self.set_content_height(640)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12);   box.set_margin_bottom(12)

        # ── Group info edit (owner only) ──
        if self._is_owner:
            info_grp = Adw.PreferencesGroup(title="Group Info")
            self._name_row = Adw.EntryRow(title="Name")
            self._name_row.set_text(group.get("name",""))
            info_grp.add(self._name_row)
            self._desc_row = Adw.EntryRow(title="Description")
            self._desc_row.set_text(group.get("description","") or "")
            info_grp.add(self._desc_row)
            save_row = Adw.ActionRow(title="Save Changes")
            save_row.set_activatable(True)
            save_row.add_suffix(Gtk.Image.new_from_icon_name("document-save-symbolic"))
            save_row.connect("activated", self._save_info)
            info_grp.add(save_row)
            box.append(info_grp)

        # ── Invite link ──
        share_grp = Adw.PreferencesGroup(title="Sharing")
        share_url = group.get("share_url","")
        invite_row = Adw.ActionRow(title="Copy Invite Link",
                                    subtitle=share_url or "No link available")
        invite_row.set_activatable(True)
        invite_row.add_suffix(Gtk.Image.new_from_icon_name("edit-copy-symbolic"))
        invite_row.connect("activated", self._copy_invite)
        share_grp.add(invite_row)
        box.append(share_grp)

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
            if secs == 0:
                row.add_css_class("success" if currently_muted else "")
            row.connect("activated", self._set_mute, gid, secs)
            notif_grp.add(row)
        box.append(notif_grp)

        # ── Admin actions ──
        admin_grp = Adw.PreferencesGroup(title="Administration")
        if self._is_owner:
            del_row = Adw.ActionRow(title="Delete Group",
                                     subtitle="Permanently remove this group")
            del_row.set_activatable(True)
            del_row.add_css_class("error")
            del_row.add_suffix(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
            del_row.connect("activated", self._delete_group)
            admin_grp.add(del_row)
        else:
            leave_row = Adw.ActionRow(title="Leave Group",
                                       subtitle="Remove yourself from this group")
            leave_row.set_activatable(True)
            leave_row.add_css_class("error")
            leave_row.add_suffix(Gtk.Image.new_from_icon_name("system-log-out-symbolic"))
            leave_row.connect("activated", self._leave_group)
            admin_grp.add(leave_row)
        box.append(admin_grp)

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    def _save_info(self, *_):
        name = self._name_row.get_text().strip()
        desc = self._desc_row.get_text().strip()
        def worker():
            r = self._api.update_group(self._group["id"], name=name, description=desc)
            GLib.idle_add(lambda: self._parent.toast(
                "Saved" if r else "Save failed"))
        threading.Thread(target=worker, daemon=True).start()

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
        threading.Thread(target=worker, daemon=True).start()

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
        threading.Thread(target=worker, daemon=True).start()


# ─────────────────────────── Preferences Dialog ──────────────────

class PreferencesDialog(Adw.Dialog):
    def __init__(self, config, parent, chat_view=None):
        super().__init__()
        self._config    = config
        self._parent    = parent
        self._chat_view = chat_view

        self.set_title("Preferences")
        self.set_content_width(400)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12);   box.set_margin_bottom(12)

        # ── Chat polling ──
        poll_grp = Adw.PreferencesGroup(
            title="Message Refresh",
            description="How often to check for new messages in the open chat")

        current_secs = int(config.get_pref("poll_interval_secs", 15))
        self._poll_spin = Adw.SpinRow.new_with_range(5, 300, 5)
        self._poll_spin.set_title("Interval (seconds)")
        self._poll_spin.set_value(current_secs)
        poll_grp.add(self._poll_spin)
        box.append(poll_grp)

        # ── Background polling ──
        bg_grp = Adw.PreferencesGroup(
            title="Background Notifications",
            description="How often to check all groups for new messages")

        current_bg = int(config.get_pref("bg_poll_interval_secs", 30))
        self._bg_spin = Adw.SpinRow.new_with_range(10, 600, 10)
        self._bg_spin.set_title("Interval (seconds)")
        self._bg_spin.set_value(current_bg)
        bg_grp.add(self._bg_spin)
        box.append(bg_grp)

        # ── Save button ──
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.add_css_class("pill")
        save_btn.connect("clicked", self._save)
        box.append(save_btn)

        tv.set_content(box)
        self.set_child(tv)

    def _save(self, *_):
        secs    = int(self._poll_spin.get_value())
        bg_secs = int(self._bg_spin.get_value())
        self._config.set_pref("poll_interval_secs",    secs)
        self._config.set_pref("bg_poll_interval_secs", bg_secs)
        if self._chat_view:
            self._chat_view.restart_poll()
        # Apply the new background-poll interval immediately instead of
        # waiting for app restart.
        if hasattr(self._parent, "_start_bg_poll"):
            self._parent._start_bg_poll()
        self._parent.toast(
            f"Poll: {secs}s  •  Background: {bg_secs}s")
        self.close()


# ─────────────────────────── Create Album Dialog ─────────────────

# ─────────────────────────── Gallery Dialog ──────────────────────

