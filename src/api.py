"""Banter — GroupMe REST API client."""

import json
import time
import urllib.request
from pathlib import Path
import urllib.parse
import urllib.error

from .constants import (
    GROUPME_API, GROUPME_IMAGE, GROUPME_POWERUPS, APP_VERSION, DEBUG, dbg, log
)


class GroupMeAPI:
    def __init__(self, token: str = None):
        self.token = token

    # ── low-level ──
    def _req(self, method: str, endpoint: str, data=None,
             params: dict = None, base: str = None):
        url = f"{base or GROUPME_API}{endpoint}"
        p = dict(params or {})
        if self.token:
            p["token"] = self.token
        if p:
            url += "?" + urllib.parse.urlencode(p)

        # Debug: log the outgoing request (redact token value)
        if DEBUG:
            safe_url = url.replace(self.token or "", "<TOKEN>") if self.token else url
            dbg("→ %s %s", method, safe_url)
            if data is not None:
                dbg("  body: %s", json.dumps(data, separators=(",", ":")))

        req = urllib.request.Request(url)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
        req.add_header("X-Access-Token", self.token or "")
        # Mimic the GroupMe web client. Reverse-engineering its JS bundle
        # showed X-Requested-With is globally injected on every request;
        # the reactions endpoint appears to gate non-heart reactions on
        # a recognized client header value.
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        req.method = method
        # ensure_ascii=False → emoji/non-ASCII chars go on the wire as
        # raw UTF-8 bytes rather than \uXXXX escapes, matching what the
        # web client sends.
        body = (json.dumps(data, ensure_ascii=False).encode("utf-8")
                if data is not None else None)

        try:
            with urllib.request.urlopen(req, body, timeout=30) as r:
                raw = r.read().decode()
                dbg("← %d  %d bytes", r.status, len(raw))
                # DELETE and some other endpoints return 204 No Content
                # with an empty body — synthesize a meta wrapper so
                # callers can use the usual _ok() / response shape.
                if not raw.strip():
                    return {"meta": {"code": r.status}, "response": None}
                parsed = json.loads(raw)
                if DEBUG:
                    code = parsed.get("meta", {}).get("code", "?")
                    dbg("  meta.code=%s", code)
                    if parsed.get("meta", {}).get("errors"):
                        dbg("  errors: %s",
                            parsed["meta"]["errors"])
                return parsed
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode()
                parsed = json.loads(raw)
                dbg("← HTTP %d  body: %s", e.code, raw[:400])
                return parsed
            except Exception:
                dbg("← HTTP %d  (non-JSON body: %s)", e.code, raw[:200])
                return {"meta": {"code": e.code, "errors": [str(e)]}}
        except Exception as e:
            dbg("← EXCEPTION: %s", e)
            log.exception("Unexpected error in _req(%s %s)", method, endpoint)
            return {"meta": {"code": 0, "errors": [str(e)]}}

    def _ok(self, r):
        return r.get("meta", {}).get("code") in (200, 201, 204)

    # ── auth / user ──
    def verify_token(self, token: str):
        """Validate an access token by calling /users/me.
        Returns (True, user_dict) on success, (False, [error_strings]) on failure."""
        self.token = token.strip()
        dbg("verify_token: calling /users/me")
        r = self._req("GET", "/users/me")
        if self._ok(r) and r.get("response"):
            user = r["response"]
            dbg("verify_token: success – user_id=%s  name=%s",
                user.get("id"), user.get("name"))
            return True, user
        errors = r.get("meta", {}).get("errors") or [
            f"HTTP {r.get('meta', {}).get('code', '?')} – token invalid or expired"
        ]
        dbg("verify_token: failed – %s", errors)
        self.token = None
        return False, errors

    def get_me(self):
        r = self._req("GET", "/users/me")
        return r.get("response")

    def update_me(self, **kwargs):
        r = self._req("POST", "/users/update", kwargs)
        return r.get("response")

    # ── groups ──
    def get_groups(self, page=1, per_page=20, omit="memberships"):
        r = self._req("GET", "/groups",
                      params={"page": page, "per_page": per_page,
                               "omit": omit})
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
        if before_id: params["before_id"] = before_id
        if since_id:  params["since_id"]  = since_id
        if after_id:  params["after_id"]  = after_id
        r = self._req("GET", f"/groups/{gid}/messages", params=params)
        return r.get("response", {}).get("messages", [])

    def send_message(self, gid, text: str, attachments=None):
        msg = {"source_guid": f"{int(time.time()*1000)}", "text": text}
        if attachments:
            msg["attachments"] = attachments
        r = self._req("POST", f"/groups/{gid}/messages", {"message": msg})
        return r.get("response", {}).get("message")

    def edit_message(self, conv_id, msg_id, text: str, attachments=None):
        """Edit an existing message.

        STATUS: GroupMe's public v3 API doesn't expose a documented
        edit endpoint, and every URL/method combo we've tried so far
        returns an HTML 500 page (Rails missing-route style, not a
        4xx with JSON — meaning the route isn't registered, not that
        the request is malformed). Until we can capture the official
        web client's actual edit request and replicate it, this
        always returns None and the UI surfaces "Edit not supported".

        Tried (all 500 HTML):
          - PUT  /v3/conversations/{cid}/messages/{mid}     {"message": {...}}
          - POST https://v2.groupme.com/messages/{cid}/{mid} {"message": {...}}
          - PATCH /v3/conversations/{cid}/messages/{mid}    {"message": {...}}
          - POST /v3/groups/{gid}/messages/{mid}/edit       {"message": {...}}
        """
        msg = {"text": text}
        if attachments is not None:
            msg["attachments"] = attachments

        attempts = (
            ("POST",  f"/messages/{conv_id}/{msg_id}",
                {"message": msg}, "https://v2.groupme.com"),
            ("PUT",   f"/conversations/{conv_id}/messages/{msg_id}",
                {"message": msg}, None),
            ("PATCH", f"/conversations/{conv_id}/messages/{msg_id}",
                {"message": msg}, None),
            ("POST",  f"/groups/{conv_id}/messages/{msg_id}/edit",
                {"message": msg}, None),
        )
        for method, path, body, base in attempts:
            r = self._req(method, path, body, base=base)
            if self._ok(r):
                return r.get("response", {}).get("message") or {"text": text}
        return None

    def delete_message(self, conv_id, msg_id):
        """Delete a message. Same `conv_id` semantics as edit_message.
        Returns True on success."""
        r = self._req("DELETE",
                       f"/conversations/{conv_id}/messages/{msg_id}")
        return self._ok(r)

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
        dbg("react_message_pack: failed pack=%s idx=%s – %s",
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

    # ── contacts ──
    def get_contacts(self):
        r = self._req("GET", "/contacts")
        return r.get("response", [])

    # ── blocks ──
    def block_user(self, me_id, other_user_id):
        """Block another user. GroupMe's /blocks endpoint takes the
        users as query parameters rather than JSON body."""
        r = self._req("POST", "/blocks",
                      params={"user":      str(me_id),
                              "otherUser": str(other_user_id)})
        return self._ok(r)

    def unblock_user(self, me_id, other_user_id):
        r = self._req("DELETE", "/blocks",
                      params={"user":      str(me_id),
                              "otherUser": str(other_user_id)})
        return self._ok(r)

    # ── direct messages ──
    def get_chats(self, page=1, per_page=20):
        """Return list of DM conversations (chats)."""
        r = self._req("GET", "/chats",
                      params={"page": page, "per_page": per_page})
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
        if before_id: params["before_id"] = before_id
        if since_id:  params["since_id"]  = since_id
        if after_id:  params["after_id"]  = after_id
        r = self._req("GET", "/direct_messages", params=params)
        return r.get("response", {}).get("direct_messages", [])

    def send_dm(self, recipient_id: str, text: str, attachments=None):
        msg = {
            "source_guid"  : f"{int(time.time()*1000)}",
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

    # ── gallery (all images sent in a group) ──
    def get_gallery(self, gid, before: str = None, after: str = None,
                    limit: int = 100):
        """Return messages that contain images, newest first.
        `before` / `after` are ISO-8601 gallery_ts strings for pagination."""
        params = {"limit": limit, "acceptFiles": "1"}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        r = self._req("GET", f"/conversations/{gid}/gallery",
                      params=params)
        resp = r.get("response", {})
        dbg("get_gallery raw type=%s", type(resp).__name__)
        if isinstance(resp, dict):
            return resp.get("messages", [])
        if isinstance(resp, list):
            return resp
        return []

    def create_album(self, gid, name: str):
        """Albums are not supported by the public API — stub retained for
        compatibility but has no server effect."""
        return None

    def get_album_photos(self, gid, album_id):
        return []

    def add_photo_to_album(self, gid, album_id, image_url: str):
        return None

    # ── calendar events ──
    def get_events(self, gid):
        r = self._req("GET", f"/conversations/{gid}/events/list")
        return r.get("response", {}).get("events", [])

    def create_event(self, gid, name: str, start_at: str,
                     end_at: str = None, location: str = "",
                     all_day: bool = False, tz: str = "UTC"):
        """Create a calendar event. GroupMe requires start_at / end_at /
        timezone / is_all_day be provided together — end_at defaults to
        start_at (zero-duration) if the caller didn't supply one."""
        payload = {
            "name"       : name,
            "start_at"   : start_at,
            "end_at"     : end_at or start_at,
            "timezone"   : tz,
            "is_all_day" : bool(all_day),
        }
        if location:
            payload["location"] = {"name": location}
        return self._req("POST",
                         f"/conversations/{gid}/events/create", payload)

    def rsvp_event(self, gid, event_id, status):
        """RSVP to an event. status: 'going' or 'not_going'."""
        r = self._req("POST",
                      f"/conversations/{gid}/events/{event_id}/rsvps",
                      {"rsvp": {"status": status}})
        return self._ok(r)

    def get_event(self, gid, event_id):
        """Return a single event dict by id, or None if not found."""
        events = self.get_events(gid) or []
        for e in events:
            if str(e.get("event_id") or e.get("id") or "") == str(event_id):
                return e
        return None

    # ── Powerups (GroupMe's proprietary emoji packs) ──
    def get_powerups(self):
        """Fetch the full GroupMe emoji-pack catalog.

        The endpoint has returned multiple shapes in the wild:
          • flat list  ................... [ {...}, {...} ]
          • flat dict  ................... {"powerups": [...]}
          • wrapped dict  ................ {"meta": ..., "response": {"powerups": [...]}}
          • wrapped list inside response . {"meta": ..., "response": [...]}
        Handle all of them, return a list of pack dicts or []."""
        r = self._req("GET", "/powerups", base=GROUPME_POWERUPS)
        if r is None:
            return []
        if isinstance(r, list):
            return r
        if isinstance(r, dict):
            if isinstance(r.get("powerups"), list):
                return r["powerups"]
            resp = r.get("response")
            if isinstance(resp, list):
                return resp
            if isinstance(resp, dict) and isinstance(resp.get("powerups"), list):
                return resp["powerups"]
        return []

    def get_event_rsvps(self, gid, event_id):
        r = self._req("GET",
                      f"/conversations/{gid}/events/{event_id}/rsvps")
        return r.get("response", {})

    # ── polls ──
    def get_polls(self, gid):
        r = self._req("GET", f"/poll/{gid}")
        return r.get("response", {}).get("polls", [])

    def create_poll(self, gid, subject: str, options: list,
                    expiry: int = 86400, multichoice: bool = False):
        r = self._req("POST", f"/poll/{gid}", {
            "subject"     : subject,
            "options"     : [{"title": o} for o in options],
            "expiration"  : expiry,
            "type"        : "multi_choice" if multichoice else "single_choice",
            "visibility"  : "public",
        })
        return r.get("response")

    def vote_poll(self, gid, poll_id: str, option_ids: list):
        r = self._req("POST", f"/poll/{gid}/{poll_id}/votes",
                      {"options": option_ids})
        return self._ok(r)

    # ── member search (for add-member) ──
    def search_users(self, query: str):
        r = self._req("GET", "/users/search",
                      params={"query": query})
        return r.get("response", [])

    # ── image upload ──
    def upload_image(self, file_path: str):
        ext = Path(file_path).suffix.lower()
        ct_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".png": "image/png",  ".gif": "image/gif",
                  ".webp": "image/webp"}
        content_type = ct_map.get(ext, "image/jpeg")

        url = f"{GROUPME_IMAGE}/pictures?token={self.token}"
        dbg("upload_image: %s  content-type=%s", file_path, content_type)

        with open(file_path, "rb") as f:
            data = f.read()
        dbg("upload_image: %d bytes to upload", len(data))

        req = urllib.request.Request(url, data=data)
        req.add_header("Content-Type", content_type)
        req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                dbg("upload_image: response %d  body=%s", resp.status, raw[:300])
                r = json.loads(raw)
                img_url = r.get("payload", {}).get("url")
                dbg("upload_image: image URL = %s", img_url)
                return img_url
        except urllib.error.HTTPError as e:
            raw = ""
            try: raw = e.read().decode()
            except Exception: pass
            dbg("upload_image: HTTP %d  %s", e.code, raw[:200])
            log.error("Image upload failed: HTTP %d", e.code)
            return None
        except Exception as e:
            dbg("upload_image: exception %s", e)
            log.exception("Image upload exception")
            return None


# ─────────────────────────── Faye Push Client ────────────────────

