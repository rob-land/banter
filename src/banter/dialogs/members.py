"""Banter — MembersDialog."""

from gi.repository import Adw, GLib, Gtk

from ..async_utils import run_in_background
from ..constants import esc
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

        # user_ids already in this group — excluded from contact suggestions
        self._existing_ids = {
            str(m.get("user_id","")) for m in group.get("members", [])}

        # uid → a group the contact belongs to, shown as suggestion subtitle
        # ("Rachel — Group X"). First group wins; cheap and good enough.
        self._group_hint: dict = {}
        self._build_group_hint()

        page = Adw.PreferencesPage()
        self.add(page)

        # ── Add member (owner only) ──
        # EntryRow's apply button (the inline ✓ arrow) is the HIG-native
        # "submit this row" affordance for the phone/email directory lookup;
        # typing a name instead live-filters contacts from every group below.
        if self._is_owner:
            add_grp = Adw.PreferencesGroup(title="Add Member")
            self._add_entry = Adw.EntryRow(
                title="Search contacts, or enter phone / email")
            self._add_entry.set_show_apply_button(True)
            self._add_entry.connect("apply", self._add_member)
            self._add_entry.connect("changed", self._on_search_changed)
            add_grp.add(self._add_entry)
            page.add(add_grp)

            # Contact suggestions, populated as the user types.
            self._suggest_grp = Adw.PreferencesGroup()
            self._suggest_grp.set_visible(False)
            page.add(self._suggest_grp)
            self._suggest_rows = []

            # Contacts may not have loaded yet if the user hasn't opened the
            # contacts tab; pull them in the background and refresh on arrival.
            if not getattr(parent, "_all_contacts", None):
                parent.ensure_contacts_loaded(self._on_contacts_ready)

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

    def _build_group_hint(self):
        for g in (getattr(self._parent, "_all_groups_with_members", None) or []):
            gname = g.get("name","")
            for m in (g.get("members") or []):
                uid = str(m.get("user_id",""))
                if uid and uid not in self._group_hint:
                    self._group_hint[uid] = gname

    def _on_contacts_ready(self):
        """Contacts finished loading after the dialog opened — pick up the
        group subtitles and re-run any in-progress search."""
        self._build_group_hint()
        self._on_search_changed(self._add_entry)

    def _on_search_changed(self, entry):
        """Live-filter contacts from every group by name as the user types."""
        for row in self._suggest_rows:
            self._suggest_grp.remove(row)
        self._suggest_rows = []

        query = entry.get_text().strip().lower()
        if not query:
            self._suggest_grp.set_visible(False)
            return

        contacts = getattr(self._parent, "_all_contacts", None) or []
        # Rank prefix matches ahead of mid-name (substring) matches.
        prefix, substr = [], []
        for c in contacts:
            uid = str(c.get("user_id",""))
            if not uid or uid in self._existing_ids:
                continue
            n_low = (c.get("name") or "").lower()
            if n_low.startswith(query):
                prefix.append(c)
            elif query in n_low:
                substr.append(c)

        matches = (prefix + substr)[:8]
        self._suggest_grp.set_visible(bool(matches))
        for c in matches:
            self._suggest_grp.add(self._make_suggest_row(c))

    def _make_suggest_row(self, contact):
        name = contact.get("name","?")
        row = Adw.ActionRow(title=esc(name))
        hint = self._group_hint.get(str(contact.get("user_id","")), "")
        if hint:
            row.set_subtitle(esc(hint))

        av = Adw.Avatar(size=36, text=esc(name), show_initials=True)
        set_avatar_from_url(av, contact.get("avatar_url",""))
        row.add_prefix(av)
        row.add_suffix(Gtk.Image.new_from_icon_name("list-add-symbolic"))

        row.set_activatable(True)
        row.connect("activated", lambda _r, c=contact: self._add_contact(c))
        self._suggest_rows.append(row)
        return row

    def _add_contact(self, contact):
        uid  = str(contact.get("user_id",""))
        name = contact.get("name","")
        if not uid:
            return
        # Suppress re-suggesting; clearing the entry also hides the list.
        self._existing_ids.add(uid)
        self._add_entry.set_text("")

        def worker():
            r = self._api.add_members(
                self._group["id"], [{"user_id": uid, "nickname": name}])
            GLib.idle_add(lambda: self._parent.toast(
                f"Added {name}" if r else "Failed to add member"))

        run_in_background(worker)

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
