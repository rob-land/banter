"""Banter — GroupMe REST API client."""

import json
import time
import urllib.request
from pathlib import Path
import urllib.parse
import urllib.error

from .constants import (
    GROUPME_API, GROUPME_IMAGE, APP_VERSION, DEBUG, dbg, log
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
        req.method = method
        body = json.dumps(data).encode() if data is not None else None

        try:
            with urllib.request.urlopen(req, body, timeout=30) as r:
                raw = r.read().decode()
                dbg("← %d  %d bytes", r.status, len(raw))
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
        return r.get("meta", {}).get("code") in (200, 201)

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

    def like_message(self, gid, msg_id):
        r = self._req("POST", f"/messages/{gid}/{msg_id}/like")
        return self._ok(r)

    def unlike_message(self, gid, msg_id):
        r = self._req("POST", f"/messages/{gid}/{msg_id}/unlike")
        return self._ok(r)

    def react_message(self, gid, msg_id, code: str):
        """Add a specific unicode emoji reaction. Falls back to heart like."""
        # GroupMe community-documented reactions endpoint
        r = self._req("POST", f"/reactions/{gid}/{msg_id}",
                      {"reaction": {"type": "unicode", "code": code}})
        if self._ok(r):
            return True
        # Fallback: use the legacy like endpoint for ❤️
        if code in ("❤️", "♥", "heart"):
            return self.like_message(gid, msg_id)
        return False

    def unreact_message(self, gid, msg_id):
        """Remove the current user's reaction from a message."""
        r = self._req("DELETE", f"/reactions/{gid}/{msg_id}")
        if self._ok(r):
            return True
        return self.unlike_message(gid, msg_id)

    # ── contacts ──
    def get_contacts(self):
        r = self._req("GET", "/contacts")
        return r.get("response", [])

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
                     all_day: bool = False):
        payload = {"event": {"name": name, "start_at": start_at,
                             "all_day": all_day}}
        if end_at:
            payload["event"]["end_at"] = end_at
        if location:
            payload["event"]["location"] = {"name": location}
        r = self._req("POST", f"/conversations/{gid}/events", payload)
        return r.get("response")

    def rsvp_event(self, gid, event_id, status):
        """RSVP to an event. status: 'going' or 'not_going'."""
        r = self._req("POST",
                      f"/conversations/{gid}/events/{event_id}/rsvps",
                      {"rsvp": {"status": status}})
        return self._ok(r)

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

