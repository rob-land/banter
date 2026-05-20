"""SendingMixin — compose-bar keystroke handling, optimistic-UI send,
retry / discard for failed sends, reply state.

Mixed into ChatView. `_send` is the central path: it stamps a synthetic
pending message, appends a pending MessageBubble immediately, then
fires the API call on a worker. The result (or push echo) transitions
the bubble in place; failure paints Retry/Discard buttons on the bubble.
"""

import time
import uuid

from gi.repository import Gdk, GLib

from ...async_utils import run_in_background
from ..message_bubble import MessageBubble


class SendingMixin:
    def set_reply_target(self, msg: dict | None):
        """Select a message to reply to (or pass None to clear). The
        compose-bar preview updates and the next send will attach a
        `reply` attachment pointing at it. Called by the message
        bubble's context menu."""
        self._reply_target = msg
        if msg is None:
            self._reply_bar.set_visible(False)
            self._reply_label.set_text("")
            return
        sender = msg.get("name") or "Unknown"
        body   = (msg.get("text") or "").strip()
        if not body:
            atts = msg.get("attachments") or []
            if any(a.get("type") == "image" for a in atts):
                body = "📷 Image"
            elif atts:
                body = "📎 Attachment"
            else:
                body = "…"
        # Single-line preview; ellipsize handles overflow visually
        self._reply_label.set_text(f"Replying to {sender}: {body}")
        self._reply_bar.set_visible(True)
        # Keyboard focus back to the entry so the user can just type
        self._entry.grab_focus()

    def _clear_pending_mentions(self):
        buf = self._entry.get_buffer()
        for entry in self._pending_mentions:
            try:
                buf.delete_mark(entry["start"])
                buf.delete_mark(entry["end"])
            except Exception:
                pass
        self._pending_mentions = []

    def _on_key(self, ctrl, keyval, keycode, state):
        # Mention popover steals navigation/accept keys while open.
        if self._mention_anchor is not None and self._mention_popover:
            if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
                self._mention_popover.navigate(-1)
                return True
            if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
                self._mention_popover.navigate(1)
                return True
            if keyval == Gdk.KEY_Escape:
                self._close_mention_popover()
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
                if self._mention_popover.accept():
                    return True
                self._close_mention_popover()
                return False

        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Return and not shift:
            self._send()
            return True
        return False

    def _send(self, *_):
        buf      = self._entry.get_buffer()
        raw_text = buf.get_text(
            buf.get_start_iter(), buf.get_end_iter(), False)
        text     = raw_text.strip()

        if not text and not self._pending_img_url and not self._pending_file_id:
            return

        # Build the mentions attachment from pending marks BEFORE we
        # clear the buffer (clearing destroys the marks). loci offsets
        # are anchored in the raw buffer text; subtract leading
        # whitespace to align them with the stripped wire text.
        leading = len(raw_text) - len(raw_text.lstrip())
        mentions_att = self._build_mentions_attachment(buf, leading)
        self._clear_pending_mentions()

        atts = []
        if self._pending_img_url:
            atts.append({"type": "image", "url": self._pending_img_url})
        elif self._pending_file_id:
            atts.append({"type": "file", "file_id": self._pending_file_id})
        if mentions_att:
            atts.append(mentions_att)
        if self._reply_target is not None:
            reply_id = str(self._reply_target.get("id", ""))
            if reply_id:
                atts.append({
                    "type":          "reply",
                    "reply_id":      reply_id,
                    "base_reply_id": reply_id,
                })

        # Optimistic UI: build a synthetic message dict and append a
        # pending bubble immediately. The user sees their message in
        # the chat instantly even if the network is slow or down — and
        # if the send ultimately fails they can retry/discard from the
        # bubble itself instead of losing their typed text.
        src_guid = uuid.uuid4().hex
        pending_id = f"pending:{src_guid}"
        pending_msg = {
            "id"          : pending_id,
            "user_id"     : str(self._me),
            "name"        : (self._win._current_user or {}).get("name", ""),
            "avatar_url"  : (self._win._current_user or {}).get("avatar_url", ""),
            "text"        : text,
            "attachments" : list(atts),
            "created_at"  : int(time.time()),
            "source_guid" : src_guid,
        }
        self._append_pending_bubble(pending_msg)

        # Now safe to wipe the compose state — the bubble holds
        # everything we need to retry on failure.
        buf.set_text("")
        self._clear_attachment()
        self.set_reply_target(None)

        # Dispatch the actual API call. Result handler transitions the
        # bubble in-place rather than appending a fresh one.
        self._dispatch_send(pending_msg)

    def _append_pending_bubble(self, msg: dict):
        d = self._msg_date(msg)
        if d != self._newest_date:
            self._msgs_box.append(self._make_date_sep(d))
            self._newest_date = d
        bubble = MessageBubble(
            msg, self._me, self._gid, self._api, self._win,
            pending=True)
        self._bubble_map[str(msg["id"])] = bubble
        self._pending_by_guid[msg["source_guid"]] = bubble
        self._msgs_box.append(bubble)
        self._scroll_bottom()

    def _dispatch_send(self, pending_msg: dict):
        """Worker-thread send. On completion, transitions the bubble
        in-place via _on_send_result."""
        text     = pending_msg["text"]
        atts     = pending_msg["attachments"] or None
        src_guid = pending_msg["source_guid"]
        is_dm    = self._is_dm
        other    = self._other_uid
        gid      = self._gid

        def worker():
            if is_dm:
                msg = self._api.send_dm(other, text, atts,
                                          source_guid=src_guid)
            else:
                msg = self._api.send_message(gid, text, atts,
                                               source_guid=src_guid)
            GLib.idle_add(self._on_send_result, src_guid, msg)

        run_in_background(worker)

    def _on_send_result(self, src_guid: str, server_msg):
        """Handle the API response for an optimistic pending send."""
        bubble = self._pending_by_guid.pop(src_guid, None)
        if bubble is None:
            # Already resolved — usually because the push echo
            # (line.create on /user/{uid}) arrived first and called
            # _resolve_pending_via_echo, which pops the entry.
            return False
        if server_msg:
            old_id = str(bubble.msg.get("id", ""))
            new_id = str(server_msg["id"])
            self._bubble_map.pop(old_id, None)
            self._bubble_map[new_id] = bubble
            bubble.transition_to_sent(server_msg)
            self._newest_id = server_msg["id"]
        else:
            bubble.transition_to_failed()
        return False   # one-shot idle_add

    def _resolve_pending_via_echo(self, subject: dict) -> bool:
        """If `subject` is the push-stream echo of a still-pending
        send, transition that bubble to sent and return True so the
        caller skips its usual append-new path. Match by source_guid
        (the temp id is client-side only)."""
        guid = str(subject.get("source_guid", ""))
        if not guid:
            return False
        bubble = self._pending_by_guid.pop(guid, None)
        if bubble is None:
            return False
        old_id = str(bubble.msg.get("id", ""))
        new_id = str(subject.get("id", ""))
        self._bubble_map.pop(old_id, None)
        self._bubble_map[new_id] = bubble
        bubble.transition_to_sent(subject)
        return True

    def retry_pending_send(self, bubble):
        """Re-enter the pending state and re-send. Keeps the original
        source_guid so the server dedupes if the original send
        actually succeeded but we lost the response."""
        bubble.transition_to_pending()
        self._pending_by_guid[bubble.msg["source_guid"]] = bubble
        self._dispatch_send(bubble.msg)

    def discard_pending(self, bubble):
        self._pending_by_guid.pop(bubble.msg.get("source_guid", ""), None)
        self._bubble_map.pop(str(bubble.msg.get("id", "")), None)
        parent = bubble.get_parent()
        if parent is not None:
            parent.remove(bubble)
