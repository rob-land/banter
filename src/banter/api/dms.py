"""GroupMe API — DM conversations + read receipts."""

import time


class DMsMixin:
    # ── direct messages ──
    def get_chats(self, page=1, per_page=20):
        """Return list of DM conversations (chats).

        Always asks for `include=unread_count` — see `get_groups` for
        why (without it the server returns None for the read fields)."""
        r = self._req("GET", "/chats",
                      params={"page": page, "per_page": per_page,
                              "include": "unread_count"})
        return r.get("response", [])

    def get_chats_all(self):
        chats, page = [], 1
        while True:
            batch = self.get_chats(page=page, per_page=20)
            chats.extend(batch)
            if len(batch) < 20:
                break
            page += 1
        return chats

    def get_dm_messages(self, other_user_id, before_id=None,
                         since_id=None, after_id=None, limit=20):
        params = {"other_user_id": other_user_id, "limit": limit}
        if before_id:
            params["before_id"] = before_id
        if since_id:
            params["since_id"] = since_id
        if after_id:
            params["after_id"] = after_id
        r = self._req("GET", "/direct_messages", params=params)
        return r.get("response", {}).get("direct_messages", [])

    def send_dm(self, recipient_id: str, text: str, attachments=None,
                source_guid: str = None):
        """Send a DM. See send_message for the source_guid contract."""
        msg = {
            "source_guid"  : source_guid or f"{int(time.time()*1000)}",
            "recipient_id" : str(recipient_id),
            "text"         : text,
        }
        if attachments:
            msg["attachments"] = attachments
        r = self._req("POST", "/direct_messages", {"direct_message": msg})
        return r.get("response", {}).get("direct_message")

    def like_dm(self, conversation_id: str, msg_id: str):
        r = self._req("POST",
                      f"/messages/{conversation_id}/{msg_id}/like")
        return self._ok(r)

    def unlike_dm(self, conversation_id: str, msg_id: str):
        r = self._req("POST",
                      f"/messages/{conversation_id}/{msg_id}/unlike")
        return self._ok(r)

    # ── read receipts ──
    def get_dm_read_receipt(self, other_user_id):
        """Return the OTHER user's last-read pointer in a DM.

        `GET /v3/direct_messages?other_user_id=X` carries a top-level
        `read_receipt` field — `{id, chat_id, message_id, user_id,
        read_at}` — whose `user_id` is the *other* participant. Use
        the message_id to decide whether each of our own bubbles in
        this DM has been seen yet.

        Limit=1 keeps the round-trip cheap; we only care about the
        receipt, not the message. Returns the receipt dict or None.

        Note: groups don't have a per-member analogue. The conversation-
        list endpoints carry only the SELF user's `last_read_message_id`
        (sidebar unread tracking), not other members'."""
        r = self._req("GET", "/direct_messages",
                      params={"other_user_id": str(other_user_id),
                              "limit": 1})
        if not self._ok(r):
            return None
        return (r.get("response") or {}).get("read_receipt")

    def read_receipt(self, conv_id: str, msg_id: str = None):
        """Mark a conversation read up to `msg_id`, or up to the
        latest message when `msg_id` is omitted.

        Two endpoints, both with an EMPTY body and the standard
        token:
            POST /v3/conversations/{cid}/read_receipt          (latest)
            POST /v3/conversations/{cid}/{mid}/read_receipt    (specific)

        `cid` is the standard conversation_id — `gid` for groups,
        `<lo>+<hi>` for DMs. Returns the parsed receipt dict
        (`{conversation_id, message_id, user_id, read_at}`) or None
        on failure. The caller doesn't usually need the returned
        message_id — it's just a confirmation."""
        if msg_id:
            path = f"/conversations/{conv_id}/{msg_id}/read_receipt"
        else:
            path = f"/conversations/{conv_id}/read_receipt"
        r = self._req("POST", path)
        if not self._ok(r):
            return None
        return (r.get("response") or {}).get("read_receipt")
