"""Banter — GroupDetailDialog, NewGroupDialog, ContactDetailDialog."""

import threading
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk

from ..constants import dbg, esc
from ..helpers import set_avatar_from_url


class GroupDetailDialog(Adw.Dialog):
    def __init__(self, api, group, me_id, parent):
        super().__init__()
        self._api    = api
        self._group  = group
        self._me     = str(me_id)
        self._parent = parent

        self.set_title(group.get("name", "Group Details"))
        self.set_content_width(420)
        self.set_content_height(680)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12);   box.set_margin_bottom(12)

        # ── Group avatar ──
        av = Adw.Avatar(size=80, text=esc(group.get("name","G")),
                        show_initials=True)
        av.set_halign(Gtk.Align.CENTER)
        set_avatar_from_url(av, group.get("image_url"))
        box.append(av)

        # ── Edit info ──
        info_grp = Adw.PreferencesGroup(title="Group Info")
        self._name_row = Adw.EntryRow(title="Name")
        self._name_row.set_text(group.get("name",""))
        info_grp.add(self._name_row)
        self._desc_row = Adw.EntryRow(title="Description")
        self._desc_row.set_text(group.get("description",""))
        info_grp.add(self._desc_row)
        box.append(info_grp)

        # ── Members ──
        self._members_grp = Adw.PreferencesGroup(title="Members")
        self._load_members()
        box.append(self._members_grp)

        # ── Actions ──
        acts = Adw.PreferencesGroup(title="Actions")

        save_row = Adw.ActionRow(title="Save Changes",
                                  subtitle="Update group name/description")
        save_row.set_activatable(True)
        save_row.add_suffix(
            Gtk.Image.new_from_icon_name("document-save-symbolic"))
        save_row.connect("activated", self._save)
        acts.add(save_row)

        invite_row = Adw.ActionRow(title="Copy Invite Link",
                                    subtitle=group.get("share_url",""))
        invite_row.set_activatable(True)
        invite_row.add_suffix(
            Gtk.Image.new_from_icon_name("edit-copy-symbolic"))
        invite_row.connect("activated", self._copy_invite)
        acts.add(invite_row)

        is_owner = str(group.get("creator_user_id","")) == self._me
        if is_owner:
            del_row = Adw.ActionRow(title="Delete Group")
            del_row.set_activatable(True)
            del_row.add_css_class("error")
            del_row.add_suffix(
                Gtk.Image.new_from_icon_name("user-trash-symbolic"))
            del_row.connect("activated", self._delete_group)
            acts.add(del_row)
        else:
            leave_row = Adw.ActionRow(title="Leave Group")
            leave_row.set_activatable(True)
            leave_row.add_css_class("error")
            leave_row.add_suffix(
                Gtk.Image.new_from_icon_name("system-log-out-symbolic"))
            leave_row.connect("activated", self._leave_group)
            acts.add(leave_row)

        box.append(acts)

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    def _load_members(self):
        members = self._group.get("members", [])
        creator = str(self._group.get("creator_user_id",""))
        is_owner = creator == self._me

        for m in members:
            name = m.get("nickname") or m.get("name","?")
            sub  = "Owner" if str(m.get("user_id","")) == creator else ""
            row  = Adw.ActionRow(title=esc(name))
            if sub:
                row.set_subtitle(sub)

            av = Adw.Avatar(size=32, text=esc(name), show_initials=True)
            set_avatar_from_url(av, m.get("image_url",""))
            row.add_prefix(av)

            if (is_owner and
                    str(m.get("user_id","")) != self._me):
                btn = Gtk.Button(icon_name="list-remove-symbolic")
                btn.add_css_class("flat")
                btn.add_css_class("destructive-action")
                btn.set_valign(Gtk.Align.CENTER)
                btn.set_tooltip_text("Remove member")
                btn.connect("clicked", self._remove_member, m)
                row.add_suffix(btn)

            self._members_grp.add(row)

    def _save(self, *_):
        name = self._name_row.get_text().strip()
        desc = self._desc_row.get_text().strip()
        def worker():
            r = self._api.update_group(self._group["id"],
                                        name=name, description=desc)
            GLib.idle_add(lambda: self._parent.toast(
                "Group updated" if r else "Update failed"))
        threading.Thread(target=worker, daemon=True).start()

    def _copy_invite(self, *_):
        url = self._group.get("share_url","")
        if url:
            Gdk.Display.get_default().get_clipboard().set(url)
            self._parent.toast("Invite link copied!")

    def _delete_group(self, *_):
        dlg = Adw.AlertDialog(
            heading="Delete Group?",
            body=f"'{self._group.get('name')}' will be permanently deleted.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete",
                                     Adw.ResponseAppearance.DESTRUCTIVE)
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
        me_id = self._me
        mid   = None
        for m in self._group.get("members",[]):
            if str(m.get("user_id","")) == me_id:
                mid = m.get("id")
                break
        if not mid:
            return
        def worker():
            ok = self._api.remove_member(self._group["id"], mid)
            GLib.idle_add(self._after_leave, ok)
        threading.Thread(target=worker, daemon=True).start()

    def _after_leave(self, ok):
        self._parent.toast("Left group" if ok else "Failed")
        if ok:
            self.close()
            self._parent.refresh_groups()

    def _remove_member(self, btn, member):
        def worker():
            ok = self._api.remove_member(
                self._group["id"], member.get("id"))
            GLib.idle_add(lambda: self._parent.toast(
                "Member removed" if ok else "Failed"))
        threading.Thread(target=worker, daemon=True).start()


class NewGroupDialog(Adw.Dialog):
    def __init__(self, api, parent):
        super().__init__()
        self._api    = api
        self._parent = parent

        self.set_title("New Group")
        self.set_content_width(380)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(16)

        grp = Adw.PreferencesGroup(title="Group Details")
        self._name = Adw.EntryRow(title="Group Name *")
        grp.add(self._name)
        self._desc = Adw.EntryRow(title="Description (optional)")
        grp.add(self._desc)
        self._share = Adw.SwitchRow(
            title="Create invite link",
            subtitle="Allow others to join via link")
        self._share.set_active(True)
        grp.add(self._share)
        box.append(grp)

        self._create_btn = Gtk.Button(label="Create Group")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.add_css_class("pill")
        self._create_btn.connect("clicked", self._create)
        box.append(self._create_btn)

        tv.set_content(box)
        self.set_child(tv)

    def _create(self, *_):
        name = self._name.get_text().strip()
        if not name:
            self._parent.toast("Group name is required")
            return
        self._create_btn.set_sensitive(False)
        self._create_btn.set_label("Creating…")

        desc  = self._desc.get_text().strip()
        share = self._share.get_active()

        def worker():
            r = self._api.create_group(name, desc, share)
            GLib.idle_add(self._on_created, r)
        threading.Thread(target=worker, daemon=True).start()

    def _on_created(self, r):
        self._create_btn.set_sensitive(True)
        self._create_btn.set_label("Create Group")
        if r:
            self._parent.toast(f"Group '{r.get('name')}' created!")
            self.close()
            self._parent.refresh_groups()
        else:
            self._parent.toast("Failed to create group")


class ContactDetailDialog(Adw.Dialog):
    def __init__(self, contact: dict, parent):
        super().__init__()
        name = contact.get("name") or contact.get("nickname","Unknown")
        self.set_title(name)
        self.set_content_width(360)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(16)

        av = Adw.Avatar(size=80, text=name, show_initials=True)
        av.set_halign(Gtk.Align.CENTER)
        url = contact.get("avatar_url") or contact.get("image_url","")
        set_avatar_from_url(av, url)
        box.append(av)

        lbl = Gtk.Label(label=name)
        lbl.add_css_class("title-2")
        lbl.set_halign(Gtk.Align.CENTER)
        box.append(lbl)

        grp = Adw.PreferencesGroup()
        if contact.get("phone_number"):
            r = Adw.ActionRow(title="Phone",
                               subtitle=contact["phone_number"])
            r.add_suffix(Gtk.Image.new_from_icon_name("call-start-symbolic"))
            grp.add(r)
        if contact.get("email"):
            r = Adw.ActionRow(title="Email",
                               subtitle=contact["email"])
            r.add_suffix(Gtk.Image.new_from_icon_name("mail-message-new-symbolic"))
            grp.add(r)
        src = contact.get("source","groupme").replace("-"," ").title()
        r2 = Adw.ActionRow(title="Source", subtitle=src)
        grp.add(r2)
        box.append(grp)

        tv.set_content(box)
        self.set_child(tv)


