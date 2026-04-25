"""Banter — GroupDetailDialog, NewGroupDialog, ContactDetailDialog."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk

from ..constants import dbg, esc
from ..async_utils import run_in_background
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
        run_in_background(worker)

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
        run_in_background(worker)

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
        run_in_background(worker)

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
        run_in_background(worker)


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
        run_in_background(worker)

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
    """Android-style contact sheet: avatar, mutual groups, actions, menu.

    Modelled after the official GroupMe Android profile screen:
      • Large avatar + name
      • Message / Add to Group action buttons
      • List of groups both users share (tap to open)
      • Overflow menu with Block / Report options
    """

    def __init__(self, api, user: dict, user_id: str, all_groups: list,
                 me_id: str, parent):
        super().__init__()
        self._api     = api
        self._user    = user
        self._uid     = str(user_id)
        self._me      = str(me_id)
        self._parent  = parent

        name = user.get("name") or user.get("nickname") or "Contact"
        self.set_title(name)
        self.set_content_width(420)
        self.set_content_height(580)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()

        # Overflow menu (Block / Report) — scoped to this dialog only
        # via a dialog-local action group so it doesn't leak into the
        # window's action namespace.
        ag = Gio.SimpleActionGroup()
        block_act = Gio.SimpleAction.new("block", None)
        block_act.connect("activate", self._confirm_block)
        ag.add_action(block_act)
        report_act = Gio.SimpleAction.new("report", None)
        report_act.connect("activate", self._do_report)
        ag.add_action(report_act)
        self.insert_action_group("contact", ag)

        menu = Gio.Menu()
        menu.append("Block user",     "contact.block")
        menu.append("Report concern", "contact.report")
        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        menu_btn.set_tooltip_text("More")
        menu_btn.set_menu_model(menu)
        hdr.pack_end(menu_btn)

        tv.add_top_bar(hdr)

        # Scrollable content
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_kinetic_scrolling(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(20)

        # Large avatar
        av = Adw.Avatar(size=96, text=esc(name), show_initials=True)
        av.set_halign(Gtk.Align.CENTER)
        set_avatar_from_url(av, user.get("avatar_url") or user.get("image_url", ""))
        box.append(av)

        # Name
        name_lbl = Gtk.Label(label=esc(name))
        name_lbl.add_css_class("title-2")
        name_lbl.set_halign(Gtk.Align.CENTER)
        name_lbl.set_wrap(True)
        name_lbl.set_max_width_chars(24)
        box.append(name_lbl)

        # Find mutual groups (needs groups with members loaded)
        mutual = []
        for g in (all_groups or []):
            for m in (g.get("members") or []):
                if str(m.get("user_id", "")) == self._uid:
                    mutual.append(g)
                    break

        # Action buttons
        btn_row = Gtk.Box(spacing=8, homogeneous=True,
                          halign=Gtk.Align.CENTER)
        btn_row.set_margin_top(4)

        dm_btn = Gtk.Button(label="Message")
        dm_btn.add_css_class("pill")
        dm_btn.add_css_class("suggested-action")
        dm_btn.connect("clicked", self._open_dm)
        btn_row.append(dm_btn)

        add_btn = Gtk.Button(label="Add to Group")
        add_btn.add_css_class("pill")
        add_btn.connect("clicked", self._open_add_to_group)
        btn_row.append(add_btn)

        box.append(btn_row)

        # Mutual groups list
        if mutual:
            heading = Gtk.Label(
                label=f"Groups you share ({len(mutual)})", xalign=0)
            heading.add_css_class("heading")
            heading.set_margin_top(8)
            box.append(heading)

            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            list_box.add_css_class("boxed-list")

            for g in mutual:
                row = Adw.ActionRow()
                row.set_title(esc(g.get("name", "Group")))
                member_count = len(g.get("members") or [])
                row.set_subtitle(f"{member_count} members")
                row.set_activatable(True)

                g_av = Adw.Avatar(size=36,
                                  text=esc(g.get("name", "G")),
                                  show_initials=True)
                set_avatar_from_url(g_av, g.get("image_url", ""))
                row.add_prefix(g_av)
                row.add_suffix(
                    Gtk.Image.new_from_icon_name("go-next-symbolic"))

                g_copy = dict(g)
                row.connect("activated",
                            lambda _r, grp=g_copy: self._open_group(grp))
                list_box.append(row)

            box.append(list_box)
        else:
            empty = Gtk.Label(label="No shared groups")
            empty.add_css_class("dim-label")
            empty.set_halign(Gtk.Align.CENTER)
            empty.set_margin_top(12)
            box.append(empty)

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    # ── Actions ──────────────────────────────────────────────────────
    def _open_dm(self, *_):
        self.close()
        self._parent.open_dm_for_user(self._user, self._uid)

    def _open_group(self, group):
        self.close()
        self._parent._current_group = group
        self._parent._open_chat(group)
        self._parent._split.set_show_content(True)

    def _open_add_to_group(self, *_):
        AddToGroupDialog(
            self._api, self._user, self._uid,
            self._parent._all_groups, self._me, self._parent,
        ).present(self._parent)

    def _confirm_block(self, *_):
        name = self._user.get("name") or "this user"
        dlg = Adw.AlertDialog(
            heading=f"Block {name}?",
            body=("You will stop receiving messages and notifications "
                  "from this user. You can unblock them later from "
                  "GroupMe's web or mobile app."),
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("block",  "Block")
        dlg.set_response_appearance(
            "block", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.connect("response", self._on_block_response)
        dlg.present(self)

    def _on_block_response(self, _dlg, response):
        if response != "block":
            return
        me, uid = self._me, self._uid
        def worker():
            ok = self._api.block_user(me, uid)
            GLib.idle_add(self._on_block_done, ok)
        run_in_background(worker)

    def _on_block_done(self, ok):
        if ok:
            self._parent.toast(
                f"Blocked {self._user.get('name', 'user')}")
            self.close()
        else:
            self._parent.toast("Block failed")

    def _do_report(self, *_):
        # GroupMe has no public report API; point the user at the
        # official channels instead of silently doing nothing.
        url = "https://groupme.com/help"
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:
            self._parent.toast(
                "To report, visit groupme.com/help")


class AddToGroupDialog(Adw.Dialog):
    """Pick a group to add this user to."""

    def __init__(self, api, user: dict, user_id: str,
                 all_groups: list, me_id: str, parent):
        super().__init__()
        self._api    = api
        self._user   = user
        self._uid    = str(user_id)
        self._me     = str(me_id)
        self._parent = parent

        name = user.get("name") or "Contact"
        self.set_title(f"Add {name}")
        self.set_content_width(400)
        self.set_content_height(520)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_kinetic_scrolling(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12);   box.set_margin_bottom(16)

        # Only offer groups where the user is NOT already a member
        candidates = []
        for g in (all_groups or []):
            members = g.get("members") or []
            if not any(str(m.get("user_id", "")) == self._uid for m in members):
                candidates.append(g)

        if not candidates:
            lbl = Gtk.Label(label=f"{name} is already in all of your groups.")
            lbl.add_css_class("dim-label")
            lbl.set_wrap(True)
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_margin_top(32)
            box.append(lbl)
        else:
            hint = Gtk.Label(
                label="Tap a group to add this contact:", xalign=0)
            hint.add_css_class("dim-label")
            box.append(hint)

            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            list_box.add_css_class("boxed-list")

            for g in candidates:
                row = Adw.ActionRow()
                row.set_title(esc(g.get("name", "Group")))
                row.set_subtitle(f"{len(g.get('members') or [])} members")
                row.set_activatable(True)

                g_av = Adw.Avatar(size=32,
                                  text=esc(g.get("name", "G")),
                                  show_initials=True)
                set_avatar_from_url(g_av, g.get("image_url", ""))
                row.add_prefix(g_av)
                row.add_suffix(
                    Gtk.Image.new_from_icon_name("list-add-symbolic"))

                g_copy = dict(g)
                row.connect("activated",
                            lambda _r, grp=g_copy: self._add_to(grp))
                list_box.append(row)

            box.append(list_box)

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    def _add_to(self, group):
        gid  = str(group["id"])
        name = self._user.get("name") or "Contact"
        members = [{
            "nickname": name,
            "user_id":  self._uid,
        }]
        def worker():
            r = self._api.add_members(gid, members)
            ok = r is not None
            GLib.idle_add(self._on_add_done, ok, group.get("name", "group"))
        run_in_background(worker)

    def _on_add_done(self, ok, group_name):
        if ok:
            self._parent.toast(
                f"Added {self._user.get('name', 'contact')} to {group_name}")
            self.close()
        else:
            self._parent.toast("Failed to add to group")


