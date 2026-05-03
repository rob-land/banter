"""Banter — MembersDialog."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib

from ..constants import esc
from ..async_utils import run_in_background
from ..helpers import set_avatar_from_url


class MembersDialog(Adw.PreferencesDialog):
    def __init__(self, api, group, me_id, parent):
        super().__init__()
        self.set_title(f"Members – {group.get('name','')}")
        self._api    = api
        self._group  = group
        self._me     = str(me_id)
        self._parent = parent
        creator      = str(group.get("creator_user_id",""))
        self._is_owner = (creator == self._me)

        page = Adw.PreferencesPage()
        self.add(page)

        # ── Add member (owner only) ──
        # EntryRow's apply button (the inline ✓ arrow) is the HIG-native
        # "submit this row" affordance; cleaner than a separate body
        # button under the entry.
        if self._is_owner:
            add_grp = Adw.PreferencesGroup(title="Add Member")
            self._add_entry = Adw.EntryRow(title="Phone number or email")
            self._add_entry.set_show_apply_button(True)
            self._add_entry.connect("apply", self._add_member)
            add_grp.add(self._add_entry)
            page.add(add_grp)

        # ── Member list ──
        self._members_grp = Adw.PreferencesGroup(
            title=f"Members ({len(group.get('members', []))})")
        page.add(self._members_grp)
        self._member_rows = []
        self._populate()

    def _populate(self):
        for row in self._member_rows:
            self._members_grp.remove(row)
        self._member_rows = []

        creator = str(self._group.get("creator_user_id",""))
        members = self._group.get("members", [])

        for m in sorted(members,
                         key=lambda x: (x.get("nickname") or x.get("name","")).lower()):
            name = m.get("nickname") or m.get("name","?")
            is_creator = str(m.get("user_id","")) == creator
            sub_parts  = []
            if is_creator:
                sub_parts.append("Owner")
            if m.get("moderator"):
                sub_parts.append("Moderator")
            sub = " · ".join(sub_parts)

            row = Adw.ActionRow(title=esc(name))
            if sub:
                row.set_subtitle(sub)

            av = Adw.Avatar(size=36, text=esc(name), show_initials=True)
            set_avatar_from_url(av, m.get("image_url",""))
            row.add_prefix(av)

            member_uid = str(m.get("user_id",""))
            if member_uid and member_uid != self._me:
                # DM button
                dm_btn = Gtk.Button(icon_name="mail-message-new-symbolic")
                dm_btn.add_css_class("flat")
                dm_btn.set_valign(Gtk.Align.CENTER)
                dm_btn.set_tooltip_text("Send direct message")
                member_info = {
                    "name"      : name,
                    "avatar_url": m.get("image_url",""),
                    "user_id"   : member_uid,
                }
                dm_btn.connect(
                    "clicked",
                    lambda _, u=member_uid, s=member_info:
                        self._parent.open_dm_for_user(s, u))
                row.add_suffix(dm_btn)

                if self._is_owner:
                    rm_btn = Gtk.Button(icon_name="list-remove-symbolic")
                    rm_btn.add_css_class("flat")
                    rm_btn.add_css_class("destructive-action")
                    rm_btn.set_valign(Gtk.Align.CENTER)
                    rm_btn.set_tooltip_text("Remove from group")
                    rm_btn.connect("clicked", self._remove_member, m)
                    row.add_suffix(rm_btn)

            self._members_grp.add(row)
            self._member_rows.append(row)

    def _add_member(self, *_):
        query = self._add_entry.get_text().strip()
        if not query:
            return
        self._add_entry.set_text("")

        def worker():
            results = self._api.search_users(query)
            if not results:
                GLib.idle_add(lambda: self._parent.toast(
                    "No users found for that query"))
                return
            # Add first matched user
            u = results[0]
            uid = str(u.get("id",""))
            members = [{"user_id": uid, "nickname": u.get("name","")}]
            r = self._api.add_members(self._group["id"], members)
            if r:
                GLib.idle_add(lambda: self._parent.toast(
                    f"Added {u.get('name','')}"))
            else:
                GLib.idle_add(lambda: self._parent.toast("Failed to add member"))

        run_in_background(worker)

    def _remove_member(self, btn, member):
        def worker():
            ok = self._api.remove_member(
                self._group["id"], member.get("id"))
            GLib.idle_add(lambda: self._parent.toast(
                "Removed" if ok else "Failed to remove"))

        run_in_background(worker)
