"""Banter — NewGroupDialog, ContactDetailDialog, AddToGroupDialog."""

from gi.repository import Adw, Gio, GLib, Gtk

from ..async_utils import run_in_background
from ..constants import esc
from ..helpers import set_avatar_from_url
from ..widgets.base import StandardDialog


class NewGroupDialog(StandardDialog):
    def __init__(self, api, parent):
        super().__init__(title="New Group", width=380, height=360)
        self._api    = api
        self._parent = parent

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        self.add_header_widget(cancel_btn, end=False)

        self._create_btn = Gtk.Button(label="Create")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.connect("clicked", self._create)
        self.add_header_widget(self._create_btn, end=True)

        box = self.set_scrolled_body(margin=16, spacing=16)

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
        self._create_btn.set_label("Create")
        if r:
            self._parent.toast(f"Group '{r.get('name')}' created!")
            self.close()
            self._parent.refresh_groups()
        else:
            self._parent.toast("Failed to create group")


class ContactDetailDialog(StandardDialog):
    """Android-style contact sheet: avatar, mutual groups, actions, menu.

    Modelled after the official GroupMe Android profile screen:
      • Large avatar + name
      • Message / Add to Group action buttons
      • List of groups both users share (tap to open)
      • Overflow menu with Block / Report options
    """

    def __init__(self, api, user: dict, user_id: str, all_groups: list,
                 me_id: str, parent):
        name = user.get("name") or user.get("nickname") or "Contact"
        super().__init__(title=name, width=420, height=580)
        self._api     = api
        self._user    = user
        self._uid     = str(user_id)
        self._me      = str(me_id)
        self._parent  = parent

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
        self.add_header_widget(menu_btn, end=True)

        box = self.set_scrolled_body(margin=16, spacing=16)

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


class AddToGroupDialog(StandardDialog):
    """Pick a group to add this user to."""

    def __init__(self, api, user: dict, user_id: str,
                 all_groups: list, me_id: str, parent):
        name = user.get("name") or "Contact"
        super().__init__(title=f"Add {name}", width=400, height=520)
        self._api    = api
        self._user   = user
        self._uid    = str(user_id)
        self._me     = str(me_id)
        self._parent = parent

        box = self.set_scrolled_body(margin=12, spacing=12)

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


