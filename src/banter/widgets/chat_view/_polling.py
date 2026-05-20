"""PollingMixin — Faye push event dispatch + DM polling fallback.

Mixed into ChatView. The host's `_start_polling` decides whether to
register the DM poll timer; the push branch is handled at the
BanterWindow level and `_on_push_event` is invoked by the window
when an event matches our group/DM channel.
"""

import logging

from gi.repository import GLib

from ...async_utils import run_in_background

log = logging.getLogger(__name__)


class PollingMixin:
    def _start_polling(self):
        """Groups use the BanterWindow-level push client.
        DMs fall back to periodic polling since push events don't
        reliably include DM group IDs."""
        if self._is_dm:
            log.debug("ChatView: using polling fallback (DM)")
            self._poll_id = GLib.timeout_add(self._poll_ms, self._poll)
        else:
            log.debug("ChatView: push handled by BanterWindow singleton")

    def _on_push_event(self, data: dict):
        """Handle a push event received from GroupMe's Faye server."""
        ev_type = data.get("type", "")
        subject = data.get("subject", {})
        log.debug("push event: type=%s subject_keys=%s", ev_type, list(subject.keys()))

        if ev_type == "typing":
            # Flat event — same shape on both /group/{gid} and
            # /direct_message/<key>: {"type":"typing","user_id":"...","started":<ms>}.
            uid = str(data.get("user_id", ""))
            if not uid or uid == str(self._me):
                return
            # In DMs, the only legitimate typer is the other party. The
            # active ChatView receives events for every subscribed DM
            # channel (Banter accumulates DM subs across the session
            # rather than unsubscribing on close), so a typing pulse
            # for *another* DM whose user_id != self._other_uid would
            # otherwise show up here as a stray indicator.
            if self._is_dm and uid != str(self._other_uid):
                return
            self._on_typing_received(uid)
            return

        if ev_type == "line.create":
            # In DMs, line.create lacks a group_id, so the gid-match
            # below skips them. The poll loop covers message
            # ingestion, but a push event from the other user is also
            # a useful signal that they're active — and may have just
            # advanced their read pointer past our latest. Kick a
            # receipt refresh so the ✓ updates without waiting for
            # the next poll tick (~15s).
            if (self._is_dm
                    and str(subject.get("user_id", "")) == str(self._other_uid)):
                self._refresh_dm_read_receipt()

            if str(subject.get("group_id", "")) == self._gid:
                # Sender finished typing — drop them from the indicator.
                sender_uid = str(subject.get("user_id", ""))
                if sender_uid and sender_uid in self._typing_users:
                    self._typing_users.pop(sender_uid, None)
                    self._refresh_typing_indicator()
                # Optimistic-send echo: if this is the push echo of a
                # message we just sent, transition the in-flight pending
                # bubble to sent rather than appending a duplicate.
                if self._resolve_pending_via_echo(subject):
                    return
                msg_id = str(subject.get("id", ""))
                if msg_id and msg_id in self._bubble_map:
                    # Already displayed (we sent it ourselves or received a duplicate)
                    # — refresh reactions in case server data differs
                    log.debug("push: line.create duplicate ignored for msg %s", msg_id)
                    self._bubble_map[msg_id].refresh(subject)
                else:
                    self._append_new([subject])

        elif ev_type in ("line.update", "message.update", "line.edit"):
            # Edit notification — replace the bubble's text and stamp
            # an "(edited)" indicator. The server may surface the new
            # text directly in `subject` or nested under `line`.
            line   = subject.get("line") or subject
            msg_id = str(line.get("id") or subject.get("line_id") or
                          subject.get("message_id") or "")
            if msg_id and msg_id in self._bubble_map:
                self._bubble_map[msg_id].update_text_from(line)

        elif ev_type == "line.destroy" or ev_type == "line.delete":
            line   = subject.get("line") or subject
            msg_id = str(line.get("id") or subject.get("line_id") or
                          subject.get("message_id") or "")
            if msg_id and msg_id in self._bubble_map:
                bubble = self._bubble_map.pop(msg_id)
                parent = bubble.get_parent()
                if parent is not None:
                    parent.remove(bubble)

        elif ev_type in ("like.create", "like.delete",
                          "favorite.create", "favorite.destroy",
                          "reaction.create", "reaction.destroy"):
            # GroupMe sends like events with the message nested under "line":
            # {"type":"like.create","subject":{"line":{msg},"reactions":[...],...}}
            line     = subject.get("line") or {}
            msg_id   = str(line.get("id") or
                           subject.get("line_id") or
                           subject.get("message_id") or
                           subject.get("id") or "")
            group_id = str(line.get("group_id") or
                           subject.get("group_id") or
                           subject.get("conversation_id") or "")
            # Log the raw reaction payload so we can see which pack / emoji
            # the originating client picked — useful for tracking down
            # packs the /powerups catalog doesn't surface.
            log.debug("push: reaction event msg_id=%s group_id=%s gid=%s in_map=%s user_reaction=%s",
                msg_id, group_id, self._gid, msg_id in self._bubble_map,
                subject.get("user_reaction"))

            if msg_id and msg_id in self._bubble_map:
                self._bubble_map[msg_id].refresh_from_server()
            elif msg_id and line and (group_id == self._gid or not group_id):
                # We have the full message in the push payload — use it directly
                # rather than making an extra API call
                bubble = self._bubble_map.get(msg_id)
                if bubble:
                    GLib.idle_add(bubble.refresh, line)

        elif ev_type == "ping":
            pass

    def _poll(self):
        """Fallback periodic poll used only when push is unavailable (DMs)."""
        if not self._newest_id:
            return True

        oldest_id = self._oldest_id
        newest_id = self._newest_id
        is_dm     = self._is_dm
        other_uid = self._other_uid
        gid       = self._gid

        def worker():
            if is_dm:
                new_msgs = self._api.get_dm_messages(
                    other_uid, since_id=newest_id, limit=20)
                refreshed = []
                # Piggyback the read-pointer fetch on the same tick.
                receipt = self._api.get_dm_read_receipt(other_uid)
            else:
                new_msgs  = self._api.get_messages(
                    gid, since_id=newest_id, limit=20)
                refreshed = self._api.get_messages(
                    gid, after_id=oldest_id, limit=20) if oldest_id else []
                receipt = None
            GLib.idle_add(self._on_poll_result, new_msgs, refreshed, receipt)

        run_in_background(worker)
        return True

    def _on_poll_result(self, new_msgs, refreshed, receipt=None):
        if new_msgs:
            self._append_new(new_msgs)
        if receipt is not None:
            self._on_dm_read_receipt(receipt)
        for msg in refreshed:
            mid = str(msg.get("id", ""))
            if mid in self._bubble_map:
                self._bubble_map[mid].refresh(msg)

    def _fetch_messages(self):
        if self._loading:
            return
        self._loading = True

        def worker():
            if self._is_dm:
                msgs = self._api.get_dm_messages(self._other_uid, limit=30)
            else:
                msgs = self._api.get_messages(self._gid, limit=30)
            GLib.idle_add(self._set_initial, msgs)

        run_in_background(worker)
