"""SidebarMixin — chats list, contacts list, sidebar search.

Mixed into BanterWindow. Owns the unified groups+DMs feed (sorted
newest-first), the contacts derived from group members, and the
visibility filter wired to the sidebar search entry.
"""

from gi.repository import GLib, Gtk

from ..async_utils import run_in_background
from ..widgets.conversation_row import ContactRow, ConversationRow


class SidebarMixin:
    # ── Unified Chats (groups + DMs) ──
    def refresh_chats(self):
        self.chats_spinner.set_spinning(True)
        self.chats_spinner.set_visible(True)

        def worker():
            groups = self._api.get_groups_all()
            chats  = self._api.get_chats_all()
            GLib.idle_add(self._display_chats, groups, chats)

        run_in_background(worker)

    # Keep refresh_groups as an alias for code that calls it (e.g. after deleting a group)
    def refresh_groups(self):
        self.refresh_chats()

    def _conv_sort_key(self, conv: dict, conv_type: str) -> int:
        """Return a unix timestamp used to sort conversations newest-first."""
        if conv_type == "dm":
            ts = (conv.get("last_message", {}).get("created_at") or
                  conv.get("updated_at") or 0)
        else:
            ts = (conv.get("messages", {}).get("last_message_created_at") or
                  conv.get("updated_at") or
                  conv.get("created_at") or 0)
        try:
            return int(ts)
        except (TypeError, ValueError):
            return 0

    def _display_chats(self, groups, chats):
        self.chats_spinner.set_spinning(False)
        self.chats_spinner.set_visible(False)
        self._all_groups = groups
        self._all_dms    = chats
        self._rows       = {}

        # Register every group with the push client so typing pulses and
        # other /group/{gid}-only events are delivered. Picked up on the
        # next /meta/connect reconnect cycle.
        if self._push is not None:
            for g in groups:
                gid = str(g.get("id", ""))
                if gid:
                    self._push.subscribe_group(gid)

        # Clear existing rows
        child = self.chats_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.chats_list.remove(child)
            child = nxt

        # Build combined list sorted by most-recent message
        items = (
            [("group", g) for g in groups] +
            [("dm",    c) for c in chats]
        )
        items.sort(key=lambda x: self._conv_sort_key(x[1], x[0]), reverse=True)

        me_id = (self._current_user or {}).get("id")
        for conv_type, conv in items:
            row = ConversationRow(conv, conv_type, self._config, me_id=me_id)
            self.chats_list.append(row)

            if conv_type == "group":
                gid = str(conv["id"])
                self._rows[self._conv_key("group", gid)] = row
                self._last_msg_ids.setdefault(
                    self._conv_key("group", gid),
                    conv.get("messages", {}).get("last_message_id"))
            else:
                other_id = str(conv.get("other_user", {}).get("id", ""))
                if other_id:
                    self._rows[self._conv_key("dm", other_id)] = row
                    self._last_msg_ids.setdefault(
                        self._conv_key("dm", other_id),
                        conv.get("last_message", {}).get("id"))

        # Update tab attention dot
        total_unread = sum(
            int(g.get("messages", {}).get("unread_count") or 0)
            for g in groups
        ) + sum(
            int(c.get("unread_count") or 0)
            for c in chats
        )
        if hasattr(self, '_chats_page'):
            self.chats_stack_page.set_needs_attention(total_unread > 0)

    def _insert_group_row(self, group: dict):
        """Add a single newly-joined group to the top of the chats list.

        Used when we learn of a group we weren't tracking — from a push
        ("you were added to …") or the background poll — without a full
        sidebar rebuild. Idempotent: returns the existing row if we
        already have one. Deliberately does NOT seed _last_msg_ids, so the
        caller's notification path still treats the first message as new."""
        gid = str(group.get("id", ""))
        if not gid:
            return None
        key = self._conv_key("group", gid)
        existing = self._rows.get(key)
        if existing is not None:
            return existing

        me_id = (self._current_user or {}).get("id")
        row = ConversationRow(group, "group", self._config, me_id=me_id)
        self.chats_list.insert(row, 0)
        self._rows[key] = row

        # Keep the cached group list consistent so contact derivation and
        # the next full rebuild both see the new group.
        if not any(str(g.get("id", "")) == gid for g in self._all_groups):
            self._all_groups.append(group)

        # Subscribe for typing pulses / group-only events.
        if self._push is not None:
            self._push.subscribe_group(gid)

        return row

    def _insert_dm_row(self, chat: dict):
        """Add a single new DM conversation to the top of the chats list.

        DM sibling of _insert_group_row, for the first-ever message from
        a new contact — learned from a push or the background poll —
        without a full sidebar rebuild. Idempotent: returns the existing
        row if we already have one. Deliberately does NOT seed
        _last_msg_ids, so the caller's notification path still treats
        the first message as new."""
        other_id = str(chat.get("other_user", {}).get("id", ""))
        if not other_id:
            return None
        key = self._conv_key("dm", other_id)
        existing = self._rows.get(key)
        if existing is not None:
            return existing

        me_id = (self._current_user or {}).get("id")
        row = ConversationRow(chat, "dm", self._config, me_id=me_id)
        self.chats_list.insert(row, 0)
        self._rows[key] = row

        # Keep the cached DM list consistent for the next full rebuild.
        # No push subscription needed — DM events arrive on the /user
        # channel we're already listening to.
        if not any(str(c.get("other_user", {}).get("id", "")) == other_id
                   for c in self._all_dms):
            self._all_dms.append(chat)

        return row

    def _on_chats_activated(self, lb, row):
        if not isinstance(row, ConversationRow):
            return
        row.set_unread(0)
        if row.conv_type == "group":
            self._current_group = row.conv
            self._open_chat(row.conv)
        else:
            other = row.conv.get("other_user", {})
            other_id = str(other.get("id", ""))
            self._open_dm(other, other_id)
        self.split.set_show_content(True)

    # ── Contacts (populated from group members) ──
    def ensure_contacts_loaded(self, on_done=None):
        """Ensure the cross-group contact caches are populated.

        Invokes on_done (on the main thread) once _all_contacts /
        _all_groups_with_members are ready — immediately if they already
        are, otherwise after a shared background fetch. Used by dialogs
        (e.g. the Members add-contact search) that read these caches
        before the user has visited the contacts tab."""
        if self._all_contacts:
            if on_done:
                on_done()
            return
        if on_done:
            self._contacts_loaded_cbs.append(on_done)
        if not self._contacts_loading:
            self._load_contacts()

    def _load_contacts(self):
        """Fetch group members in the background and populate the contacts tab."""
        self._contacts_loading = True
        self.contacts_spinner.set_spinning(True)
        self.contacts_spinner.set_visible(True)

        def worker():
            # Fetch groups WITH members (no omit=memberships)
            groups_with_members = self._api.get_groups_all_with_members()
            GLib.idle_add(self._populate_contacts_from_groups,
                          groups_with_members)

        run_in_background(worker)

    def _populate_contacts_from_groups(self, groups):
        """Build the contacts list from all group members across all groups."""
        # Keep the full-member copy around — the contact detail sheet
        # needs it to compute mutual groups without a second fetch.
        self._all_groups_with_members = groups or []
        me_id = str(self._current_user.get("id", "")) if self._current_user else ""

        seen: dict = {}   # user_id → contact dict
        for g in groups:
            for m in (g.get("members") or []):
                uid = str(m.get("user_id", ""))
                if not uid or uid == me_id:
                    continue
                name = m.get("nickname") or m.get("name") or uid
                if uid not in seen:
                    seen[uid] = {
                        "user_id"   : uid,
                        "name"      : name,
                        "avatar_url": m.get("image_url", ""),
                    }
                else:
                    # Prefer entries with an avatar
                    if not seen[uid].get("avatar_url") and m.get("image_url"):
                        seen[uid]["avatar_url"] = m["image_url"]

        contacts = sorted(seen.values(),
                          key=lambda c: (c.get("name") or "").lower())
        self._display_contacts(contacts)

    def _display_contacts(self, contacts):
        self.contacts_spinner.set_spinning(False)
        self.contacts_spinner.set_visible(False)
        self._all_contacts = contacts
        # Release any waiters parked in ensure_contacts_loaded().
        self._contacts_loading = False
        cbs, self._contacts_loaded_cbs = self._contacts_loaded_cbs, []
        for cb in cbs:
            cb()
        # Populate name cache from contact list
        for c in contacts:
            uid = str(c.get("user_id", ""))
            name = c.get("name", "")
            if uid and name:
                self._name_cache[uid] = name

        child = self.contacts_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.contacts_list.remove(child)
            child = nxt

        if not contacts:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            lbl = Gtk.Label(label="No contacts found in your groups")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(32)
            lbl.set_margin_bottom(32)
            row.set_child(lbl)
            self.contacts_list.append(row)
            return

        for c in contacts:
            self.contacts_list.append(ContactRow(c))

    # ── Selection handler ──
    def _on_contact_activated(self, lb, row):
        if not isinstance(row, ContactRow):
            return
        c        = row.contact
        other_id = str(c.get("user_id") or c.get("id") or "")
        self.open_dm_for_user(c, other_id)

    # ── Search ──
    def _on_search(self, entry):
        q = entry.get_text().lower()

        child = self.chats_list.get_first_child()
        while child:
            if isinstance(child, ConversationRow):
                conv = child.conv
                if child.conv_type == "group":
                    n = conv.get("name", "").lower()
                    d = conv.get("description", "").lower()
                    child.set_visible(not q or q in n or q in d)
                else:
                    n = (conv.get("other_user", {}).get("name", "")).lower()
                    p = (conv.get("last_message", {}).get("text", "") or "").lower()
                    child.set_visible(not q or q in n or q in p)
            child = child.get_next_sibling()

        child = self.contacts_list.get_first_child()
        while child:
            if isinstance(child, ContactRow):
                n = (child.contact.get("name") or "").lower()
                p = (child.contact.get("phone_number") or "").lower()
                e = (child.contact.get("email") or "").lower()
                child.set_visible(not q or q in n or q in p or q in e)
            child = child.get_next_sibling()
