"""Banter — MembersDialog."""

import threading
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gdk

from ..constants import esc
from ..helpers import set_avatar_from_url


class MembersDialog(Adw.Dialog):
    def __init__(self, api, group, me_id, parent):
        super().__init__()
        self._api    = api
        self._group  = group
        self._me     = str(me_id)
        self._parent = parent
        creator      = str(group.get("creator_user_id",""))
        self._is_owner = (creator == self._me)

        self.set_title(f"Members – {group.get('name','')}")
        self.set_content_width(400)
        self.set_content_height(600)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        tv.add_top_bar(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12);   box.set_margin_bottom(12)

        # ── Add member (owner only) ──
        if self._is_owner:
            add_grp = Adw.PreferencesGroup(title="Add Member")
            self._add_entry = Adw.EntryRow(title="Phone number or email")
            add_grp.add(self._add_entry)
            add_btn = Gtk.Button(label="Add")
            add_btn.add_css_class("suggested-action")
            add_btn.add_css_class("pill")
            add_btn.set_margin_top(6)
            add_btn.connect("clicked", self._add_member)
            box.append(add_grp)
            box.append(add_btn)

        # ── Member list ──
        self._members_grp = Adw.PreferencesGroup(
            title=f"Members ({len(group.get('members', []))})")
        box.append(self._members_grp)
        self._populate()

        scroll.set_child(box)
        tv.set_content(scroll)
        self.set_child(tv)

    def _populate(self):
        # Clear existing rows
        child = self._members_grp.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            try: self._members_grp.remove(child)
            except Exception: pass
            child = nxt

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

        threading.Thread(target=worker, daemon=True).start()

    def _remove_member(self, btn, member):
        def worker():
            ok = self._api.remove_member(
                self._group["id"], member.get("id"))
            GLib.idle_add(lambda: self._parent.toast(
                "Removed" if ok else "Failed to remove"))

        threading.Thread(target=worker, daemon=True).start()


# ─────────────────────────── Group Settings Dialog ───────────────

