"""GroupMe API — groups, members, messages, pins, reactions.

Everything that operates on a group conversation. Mixed into
`GroupMeAPI` by `api/__init__.py`.
"""

import logging
import time

log = logging.getLogger(__name__)


class GroupsMixin:
    # ── groups ──
    def get_groups(self, page=1, per_page=20, omit="memberships"):
        # `include=unread_count` is what gets the server to return a
        # real integer for `messages.unread_count` / `last_read_*`
        # fields. Without it the values come back as None — the web
        # client always sends this, and the sidebar unread badge is
        # broken without it.
        r = self._req("GET", "/groups",
                      params={"page": page, "per_page": per_page,
                               "omit": omit,
                               "include": "unread_count"})
        return r.get("response", [])

    def get_groups_all(self):
        groups, page = [], 1
        while True:
            batch = self.get_groups(page=page, per_page=20)
            groups.extend(batch)
            if len(batch) < 20:
                break
            page += 1
        return groups

    def get_groups_all_with_members(self):
        """Fetch all groups including the members list (no omit parameter).
        Slower than get_groups_all — used for populating contacts."""
        groups, page = [], 1
        while True:
            batch = self.get_groups(page=page, per_page=20, omit="")
            groups.extend(batch)
            if len(batch) < 20:
                break
            page += 1
        return groups

    def get_members(self, gid):
        """Fetch just the members list for a single group."""
        r = self._req("GET", f"/groups/{gid}/members")
        return r.get("response", [])

    def get_group(self, gid):
        r = self._req("GET", f"/groups/{gid}")
        return r.get("response")

    def create_group(self, name: str, description: str = "",
                     share: bool = True):
        r = self._req("POST", "/groups",
                      {"name": name, "description": description,
                       "share": share})
        return r.get("response")

    def update_group(self, gid, **kwargs):
        r = self._req("POST", f"/groups/{gid}/update", kwargs)
        return r.get("response")

    def destroy_group(self, gid):
        r = self._req("POST", f"/groups/{gid}/destroy")
        return self._ok(r)

    def join_group(self, gid, share_token):
        r = self._req("POST", f"/groups/{gid}/join/{share_token}")
        return r.get("response")

    def rejoin_group(self, gid):
        r = self._req("POST", f"/groups/{gid}/rejoin")
        return r.get("response")

    def change_owners(self, requests):
        r = self._req("POST", "/groups/change_owners", {"requests": requests})
        return r.get("response")

    # ── members ──
    def add_members(self, gid, members: list):
        r = self._req("POST", f"/groups/{gid}/members/add",
                      {"members": members})
        return r.get("response")

    def remove_member(self, gid, membership_id):
        r = self._req("POST",
                      f"/groups/{gid}/members/{membership_id}/remove")
        return self._ok(r)

    def update_membership(self, gid, **kwargs):
        r = self._req("POST", f"/groups/{gid}/memberships/update", kwargs)
        return r.get("response")

    # ── messages ──
    def get_messages(self, gid, before_id=None, since_id=None,
                     after_id=None, limit=20):
        params = {"limit": limit}
        if before_id:
            params["before_id"] = before_id
        if since_id:
            params["since_id"] = since_id
        if after_id:
            params["after_id"] = after_id
        r = self._req("GET", f"/groups/{gid}/messages", params=params)
        return r.get("response", {}).get("messages", [])

    def send_message(self, gid, text: str, attachments=None,
                     source_guid: str = None):
        """Send a group message. `source_guid` lets the caller force a
        specific dedup key — pass through the same value on a retry and
        GroupMe will silently dedupe if the original send actually
        succeeded but the response was lost."""
        msg = {
            "source_guid": source_guid or f"{int(time.time()*1000)}",
            "text"       : text,
        }
        if attachments:
            msg["attachments"] = attachments
        r = self._req("POST", f"/groups/{gid}/messages", {"message": msg})
        return r.get("response", {}).get("message")

    def edit_message(self, conv_id, msg_id, text: str, attachments=None):
        """Edit an existing group message.

        Recovered from the official GroupMe Web client (29 Apr 2026):
            PUT https://api.groupme.com/v4/groups/{gid}/messages/{mid}
            Body (UNWRAPPED — no "message" key): {"text":"...","attachments":[...]}

        Note: this is the v4 endpoint, not v3 — the rest of the API is
        v3 but this single call uses the newer prefix. DMs likely use
        an analogous /v4/conversations/{cid}/messages/{mid} but we
        haven't captured that one yet.

        Returns the updated message dict on success, None on failure.
        """
        body = {"text": text, "attachments": attachments or []}
        r = self._req("PUT",
                       f"/groups/{conv_id}/messages/{msg_id}",
                       body,
                       base="https://api.groupme.com/v4")
        if self._ok(r):
            return r.get("response", {}).get("message") or {"text": text}
        # DM fallback (untested) — same shape under /conversations/.
        r = self._req("PUT",
                       f"/conversations/{conv_id}/messages/{msg_id}",
                       body,
                       base="https://api.groupme.com/v4")
        if self._ok(r):
            return r.get("response", {}).get("message") or {"text": text}
        return None

    def delete_message(self, conv_id, msg_id):
        """Delete a message. Same `conv_id` semantics as edit_message.
        Returns True on success."""
        r = self._req("DELETE",
                       f"/conversations/{conv_id}/messages/{msg_id}")
        return self._ok(r)

    def pin_message(self, conv_id, msg_id):
        """Pin a message. Recovered from web.groupme.com (30 Apr 2026):
            POST /v3/conversations/{conv_id}/messages/{mid}/pin
        Empty body. Same `conv_id` semantics as delete_message — group_id
        for groups, '<lo>+<hi>' for DMs. Returns True on success."""
        r = self._req("POST",
                      f"/conversations/{conv_id}/messages/{msg_id}/pin")
        return self._ok(r)

    def unpin_message(self, conv_id, msg_id):
        """Unpin a message. Mirror of pin_message; same path with /unpin."""
        r = self._req("POST",
                      f"/conversations/{conv_id}/messages/{msg_id}/unpin")
        return self._ok(r)

    def get_pinned_group(self, gid):
        """Return the list of currently-pinned messages in a group."""
        r = self._req("GET", f"/pinned/groups/{gid}/messages")
        return r.get("response", {}).get("messages", []) or []

    def get_pinned_dm(self, other_user_id):
        """Return the list of currently-pinned messages in a DM."""
        r = self._req("GET", "/pinned/direct_messages",
                      params={"other_user_id": str(other_user_id)})
        return r.get("response", {}).get("direct_messages", []) or []

    # ── reactions ──
    def like_message(self, gid, msg_id):
        r = self._req("POST", f"/messages/{gid}/{msg_id}/like")
        return self._ok(r)

    def unlike_message(self, gid, msg_id):
        r = self._req("POST", f"/messages/{gid}/{msg_id}/unlike")
        return self._ok(r)

    def react_message(self, gid, msg_id, code: str):
        """Add a unicode emoji reaction to a message.

        Verified against the GroupMe web client: POST to the overloaded
        /like endpoint with a `like_icon` body. The `type`/`code` shape
        inside like_icon matches the reaction schema used in responses."""
        r = self._req("POST", f"/messages/{gid}/{msg_id}/like",
                      {"like_icon": {"type": "unicode", "code": code}})
        if self._ok(r):
            return True
        # Fallback: plain heart-only /like for resilience
        if code in ("❤️", "♥", "heart"):
            return self.like_message(gid, msg_id)
        return False

    def react_message_pack(self, gid, msg_id, pack_id, pack_index):
        """Add a GroupMe powerup (pack) emoji reaction.

        Body shape mirrors the received-reaction shape recorded in
        emoji_reactions.log:
          {"like_icon":
            {"type": "emoji", "pack_id": "N", "pack_index": "N"}}

        Returns (ok: bool, error_hint: str|None)."""
        r = self._req("POST", f"/messages/{gid}/{msg_id}/like", {
            "like_icon": {
                "type":       "emoji",
                "pack_id":    str(pack_id),
                "pack_index": str(pack_index),
            },
        })
        if self._ok(r):
            return True, None
        meta = (r or {}).get("meta", {})
        errs = meta.get("errors") or []
        hint = errs[0] if errs else f"HTTP {meta.get('code', '?')}"
        log.debug("react_message_pack: failed pack=%s idx=%s – %s",
            pack_id, pack_index, hint)
        return False, hint

    def unreact_message(self, gid, msg_id):
        """Remove the current user's reaction from a message.

        Web client issues a plain POST to /unlike — no body required.
        The server uses the caller's user id to figure out which
        reaction to drop."""
        r = self._req("POST", f"/messages/{gid}/{msg_id}/unlike")
        if self._ok(r):
            return True
        return self.unlike_message(gid, msg_id)
