"""BackgroundPollMixin — periodic /groups + /chats sweep, notifications.

Mixed into BanterWindow. Fires every BG_POLL_INTERVAL_MS to detect
new messages, refresh sidebar previews, and surface desktop
notifications for conversations that aren't currently open. Push
events handle the real-time path; this poll catches up after a
sleep / network outage and corrects unread badges that pushed
clients have already read.
"""

import logging

from gi.repository import GLib

from ..async_utils import run_in_background
from ..constants import DEMO
from ..helpers import format_preview

log = logging.getLogger(__name__)


class BackgroundPollMixin:
    BG_POLL_INTERVAL_MS = 30_000

    def _start_bg_poll(self):
        if DEMO:
            return
        if self._bg_poll_id:
            GLib.source_remove(self._bg_poll_id)
        self._bg_poll_id = GLib.timeout_add(
            self.BG_POLL_INTERVAL_MS, self._bg_poll)

    def _stop_bg_poll(self):
        if self._bg_poll_id:
            GLib.source_remove(self._bg_poll_id)
            self._bg_poll_id = None

    def _bg_poll(self):
        """Fetch all group/DM summaries to detect new messages."""
        if not self._api:
            return False

        def worker():
            groups = self._api.get_groups_all()
            chats  = self._api.get_chats_all()
            GLib.idle_add(self._process_bg_update, groups, chats)

        run_in_background(worker)
        return True   # keep timer alive

    def _is_conv_open(self, conv_type: str, conv_id) -> bool:
        """Whether the active ChatView is showing this conversation.
        Uses ChatView's own state rather than `_current_group`, which
        is only ever set for groups (not DMs)."""
        cv = self._chat_view
        if cv is None:
            return False
        cid = str(conv_id)
        if conv_type == "dm":
            return bool(getattr(cv, "_is_dm", False)
                        and str(getattr(cv, "_other_uid", "")) == cid)
        return bool(not getattr(cv, "_is_dm", False)
                    and str(getattr(cv, "_gid", "")) == cid)

    def _process_bg_update(self, groups, chats):
        # The /groups and /chats responses both carry an `unread_count`
        # field, but in practice the value is `None` (not an integer)
        # for the vast majority of conversations — relying on it as
        # the notification gate caused DM notifications to never fire
        # at all. Instead we use "last_message_id changed since the
        # previous poll" as the new-message signal, plus a self-echo
        # filter so I don't get a notification for messages I just
        # sent. The local count of unread badges is also derived from
        # the same signal — there's no exact unread count without
        # fetching messages, so we just toggle a sidebar dot.
        me_id   = str((self._current_user or {}).get("id", ""))
        my_name = (self._current_user or {}).get("name", "")
        any_unread = False

        # ── Groups ──
        for g in groups:
            gid      = str(g["id"])
            key      = self._conv_key("group", gid)
            row      = self._rows.get(key)
            msgs     = g.get("messages", {})
            last_id  = msgs.get("last_message_id")
            prev_id  = self._last_msg_ids.get(key)

            # A group with no sidebar row is one we were added to since the
            # last load — materialise it at the top. This is the catch-up
            # path for when the push "you were added" event never arrived.
            if row is None:
                row = self._insert_group_row(g)

            if not (last_id and last_id != prev_id):
                continue
            self._last_msg_ids[key] = last_id

            # Move to top of unified chats list + refresh preview/time
            preview = msgs.get("preview", {}) or {}
            preview_text = format_preview(preview.get("text"),
                                          preview.get("attachments"))
            if row is not None:
                self.chats_list.remove(row)
                self.chats_list.insert(row, 0)
                ts = msgs.get("last_message_created_at")
                if ts:
                    row.update_time(ts)
                row.update_preview(preview.get("nickname", ""), preview_text)

            # Self-echo filter: group preview has nickname but no
            # user_id, so we fall back to comparing against our own
            # display name. Imperfect (a member with the same name
            # would be filtered too) but the failure mode is "missed
            # notification on a name collision" which is benign.
            sender = preview.get("nickname", "")
            from_me = bool(my_name) and sender == my_name
            if from_me or self._is_conv_open("group", gid):
                continue
            any_unread = True
            if row is not None:
                row.bump_unread()
            notif_text = preview_text or "📎 attachment"
            self._send_desktop_notification(
                g.get("name", "GroupMe"),
                f"{sender}: {notif_text}" if sender else notif_text,
                tag=f"group-{gid}")

        # ── DMs ──
        log.debug("bg_poll: %d chats received", len(chats))
        for chat in chats:
            other_id = str(chat.get("other_user", {}).get("id", ""))
            key      = self._conv_key("dm", other_id)
            row      = self._rows.get(key)
            lm       = chat.get("last_message", {}) or {}
            last_id  = lm.get("id")
            prev_id  = self._last_msg_ids.get(key)

            if not last_id:
                log.debug("bg_poll: dm %s has no last_message.id, skipping", other_id)
                continue
            if last_id == prev_id:
                continue
            log.debug("bg_poll: dm %s NEW last_id=%s (prev=%s)",
                other_id, last_id, prev_id)
            self._last_msg_ids[key] = last_id

            # Move to top + refresh preview/time
            preview_text = format_preview(lm.get("text"), lm.get("attachments"))
            if row is not None:
                self.chats_list.remove(row)
                self.chats_list.insert(row, 0)
                ts = lm.get("created_at")
                if ts:
                    row.update_time(ts)
                sender_id = str(lm.get("sender_id") or lm.get("user_id") or "")
                if me_id and sender_id == me_id:
                    sender_for_preview = "You"
                else:
                    sender_for_preview = chat.get("other_user", {}).get("name", "")
                row.update_preview(sender_for_preview, preview_text)

            # Self-echo filter: DM last_message has user_id, so this
            # is exact (unlike groups).
            sender_id = str(lm.get("sender_id") or lm.get("user_id") or "")
            from_me   = bool(me_id) and sender_id == me_id
            is_open   = self._is_conv_open("dm", other_id)
            log.debug("bg_poll: dm %s sender=%s me=%s from_me=%s open=%s",
                other_id, sender_id, me_id, from_me, is_open)
            if from_me or is_open:
                continue
            any_unread = True
            if row is not None:
                row.bump_unread()
            other_name = chat.get("other_user", {}).get("name", "Someone")
            self._send_desktop_notification(
                other_name, preview_text or "📎 attachment",
                tag=f"dm-{other_id}")

        # Sync each row's badge to the server's authoritative unread
        # count. Push events drive `bump_unread()` for instant feedback;
        # this pass corrects drift from other clients (a sibling phone
        # opening the chat clears unread there, and we want our count
        # to follow). Skipped for the currently-open conv — opening a
        # chat fires a read_receipt but the server may not have
        # processed it by the time this poll's response was built, so
        # we'd briefly re-display a stale count. The chat-open flow
        # already zeroes the row.
        for g in groups:
            gid = str(g["id"])
            row = self._rows.get(self._conv_key("group", gid))
            if row is None or self._is_conv_open("group", gid):
                continue
            sc = (g.get("messages") or {}).get("unread_count")
            if sc is not None:
                row.set_unread(int(sc))
        for chat in chats:
            other_id = str(chat.get("other_user", {}).get("id", ""))
            row = self._rows.get(self._conv_key("dm", other_id))
            if row is None or self._is_conv_open("dm", other_id):
                continue
            sc = chat.get("unread_count")
            if sc is not None:
                row.set_unread(int(sc))

        # Tab attention dot reflects persistent unread state, not just
        # "any new this poll" — derive from the per-row counters that
        # bump_unread / set_unread maintain.
        any_unread_persistent = any(
            getattr(r, "_unread_count_n", 0) > 0
            for r in self._rows.values())
        if hasattr(self, '_chats_page'):
            self.chats_stack_page.set_needs_attention(any_unread_persistent)

    def _send_desktop_notification(self, title: str, body: str,
                                    tag: str = "banter-msg",
                                    buttons: list = None,
                                    mute_key: str = None):
        self._notifications.send(title, body, tag=tag,
                                 buttons=buttons, mute_key=mute_key)

    def _withdraw_notification(self, tag: str):
        self._notifications.withdraw(tag)

    def _handle_call_event_notification(self, conv_type: str,
                                        conv_id: str, subject: dict) -> bool:
        # Resolve the conversation's display name from the sidebar row,
        # then hand off to the dispatcher (which has no view of _rows).
        row = self._rows.get(self._conv_key(conv_type, conv_id))
        if row and conv_type == "group":
            title = (row.conv or {}).get("name") or "Group call"
        elif row and conv_type == "dm":
            other = (row.conv or {}).get("other_user") or {}
            title = other.get("name") or "Call"
        else:
            title = ""
        return self._notifications.handle_call_event(
            conv_type, conv_id, subject, conv_title=title)
