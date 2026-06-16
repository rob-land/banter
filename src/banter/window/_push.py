"""PushMixin — application-level Faye push client + event routing.

Mixed into BanterWindow. Owns the single GroupMePush instance for
the session, dispatches incoming events to the active ChatView, and
maintains sidebar previews / unread badges / poll-card live state
based on `line.create` and `poll.vote` events.
"""

import logging

from gi.repository import GLib

from ..async_utils import run_in_background
from ..constants import DEMO
from ..helpers import format_preview, is_hidden_system_message
from ..push import GroupMePush

log = logging.getLogger(__name__)


class PushMixin:
    def _start_push(self):
        """Create and start the shared push client for this session."""
        if DEMO:
            return
        if self._push or not self._api:
            return
        user_id = str(self._current_user.get("id", ""))
        if not user_id:
            return
        self._push = GroupMePush(
            self._api.token, user_id,
            on_event=self._on_push_event,
            on_error=lambda m: log.debug("push error: %s", m),
        )
        self._push.start()
        log.debug("BanterWindow: push client started for user %s", user_id)

    def _stop_push(self):
        if self._push:
            self._push.stop()
            self._push = None

    # ── Poll card registry (live-update wiring) ──
    def register_poll_card(self, poll_id, card):
        """Called by PollCard.__init__ so push poll.vote events can
        find the live widget. The list is cleared whenever a new
        ChatView is built, so we don't accumulate dead cards."""
        if not poll_id:
            return
        self._poll_cards.setdefault(str(poll_id), []).append(card)

    def _clear_poll_cards(self):
        self._poll_cards.clear()

    def _on_push_event(self, data: dict):
        """Route push events to the active ChatView (if any)."""
        ev_type = data.get("type", "")
        subject = data.get("subject", {})
        log.debug("push event: type=%s", ev_type)

        # Live poll vote — push fires on every vote (own + others) and
        # carries the full poll snapshot under subject.poll.data. Hand
        # it to any mounted PollCards for the same poll_id.
        if ev_type == "poll.vote":
            poll_data = (subject.get("poll") or {}).get("data") or {}
            pid = str(poll_data.get("id", ""))
            for card in list(self._poll_cards.get(pid, ())):
                card.apply_push_update(poll_data)
            return

        # Delegate to the currently open ChatView
        if self._chat_view:
            self._chat_view._on_push_event(data)

        # Also keep the sidebar unread counts fresh on new messages.
        # `direct_message.create` is the DM-specific event name some
        # endpoints emit; we accept both that and a `line.create`
        # without a group_id (the DM form on /user/{uid}).
        if ev_type in ("line.create", "direct_message.create"):
            if is_hidden_system_message(subject):
                return

            gid = str(subject.get("group_id", ""))
            if gid:
                self._handle_group_push_message(gid, subject)
            else:
                self._handle_dm_push_message(subject)

    def _handle_group_push_message(self, gid: str, subject: dict):
        key = self._conv_key("group", gid)
        row = self._rows.get(key)
        if row is None:
            # A push for a group we aren't tracking means we were just
            # added to it. Fetch its details, drop a row at the top of
            # the sidebar, then replay this event so the normal preview /
            # unread / notification path runs against the new row.
            self._fetch_new_group(gid, subject)
            return
        sender = subject.get("name", "")
        # Run preview text through format_preview so the server's
        # voice-note downgrade warning is replaced with a concise
        # "🎤 Voice message" instead of dominating the sidebar.
        preview_text = format_preview(subject.get("text"),
                                      subject.get("attachments"))

        # Always move to top, update preview + time — keeps the
        # sidebar in most-recent order regardless of which chat
        # is currently open.
        self.chats_list.remove(row)
        self.chats_list.insert(row, 0)
        ts = subject.get("created_at")
        if ts:
            row.update_time(ts)
        row.update_preview(sender, preview_text)

        # Mirror the bg_poll's _last_msg_ids bookkeeping so the next
        # poll doesn't re-fire a notification for the same message.
        msg_id = str(subject.get("id", ""))
        if msg_id:
            self._last_msg_ids[key] = msg_id

        # Call-started/ended events get a richer notification with a
        # Join button (and we surface them even when the chat is open).
        if self._handle_call_event_notification("group", gid, subject):
            return

        if self._is_conv_open("group", gid):
            return
        row.bump_unread()
        name = (row.conv or {}).get("name", "GroupMe")
        self._send_desktop_notification(
            name, f"{sender}: {preview_text or '📎 attachment'}",
            tag=f"group-{gid}")

    def _fetch_new_group(self, gid: str, subject: dict):
        """Background-fetch a group we were just added to, then replay the
        triggering push event once its sidebar row exists."""
        if gid in self._pending_group_adds:
            return   # a concurrent push already kicked off the fetch
        self._pending_group_adds.add(gid)

        def worker():
            group = self._api.get_group(gid)
            GLib.idle_add(self._on_new_group, gid, group, subject)

        run_in_background(worker)

    def _on_new_group(self, gid: str, group: dict, subject: dict):
        self._pending_group_adds.discard(gid)
        if self._conv_key("group", gid) in self._rows:
            # A full refresh (or the bg poll) added it while we fetched.
            self._handle_group_push_message(gid, subject)
            return
        if not group:
            return
        self._insert_group_row(group)
        # Re-run the handler now that the row exists; it sets the preview,
        # bumps unread and fires the desktop notification for the message
        # that revealed the new group ("… added you to the group").
        self._handle_group_push_message(gid, subject)

    def _handle_dm_push_message(self, subject: dict):
        """Real-time DM notification path. Without this, DMs only
        notified via the 30 s bg_poll because the push event lacks
        a group_id and the legacy handler skipped it."""
        sender_uid = str(subject.get("user_id", "") or
                          subject.get("sender_id", ""))
        me_id = str((self._current_user or {}).get("id", ""))
        if not sender_uid or sender_uid == me_id:
            return   # self-echo of an outgoing send

        # The other party in this DM, from my perspective, is whoever
        # the sender is (since they're the participant that isn't me).
        other_id = sender_uid
        key      = self._conv_key("dm", other_id)
        row      = self._rows.get(key)

        sender = subject.get("name") or "Someone"
        preview_text = format_preview(subject.get("text"),
                                      subject.get("attachments"))

        if row is not None:
            self.chats_list.remove(row)
            self.chats_list.insert(row, 0)
            ts = subject.get("created_at")
            if ts:
                row.update_time(ts)
            row.update_preview(sender, preview_text)

        # Mirror bg_poll bookkeeping so the next /chats poll doesn't
        # double-fire a notification for the same message.
        msg_id = str(subject.get("id", ""))
        if msg_id:
            self._last_msg_ids[key] = msg_id

        # Same call-event handling as groups — DMs may not actually
        # carry call events in practice (no HAR evidence), but the
        # event shape would be identical so the handler is a no-op
        # cost and gives us coverage if it ever fires.
        if self._handle_call_event_notification("dm", other_id, subject):
            return

        if self._is_conv_open("dm", other_id):
            return
        if row is not None:
            row.bump_unread()
        self._send_desktop_notification(
            sender, preview_text or "📎 attachment", tag=f"dm-{other_id}")
