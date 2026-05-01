"""Banter — GroupMe REST API client."""

import json
import mimetypes
import time
import urllib.request
from pathlib import Path
import urllib.parse
import urllib.error

from .constants import (
    GROUPME_API, GROUPME_IMAGE, GROUPME_POWERUPS, GROUPME_FILE,
    APP_VERSION, DEBUG, dbg, log
)


class GroupMeAPI:
    # Retry config for `_req`. Only GETs are retried (writes use
    # source_guid for idempotency on the GroupMe side, but a few
    # endpoints — like /like — don't, so playing it safe).
    RETRY_BACKOFF_S    = (0.5, 1.5)   # seconds between attempts
    RETRY_HTTP_CODES   = (502, 503, 504)

    def __init__(self, token: str = None, on_unauthorized=None,
                 on_online=None, on_offline=None):
        self.token = token
        # Optional callback fired ONCE when a token-bearing request
        # comes back HTTP 401. Lets the UI layer surface a session-
        # expired prompt rather than letting every subsequent call
        # silently fail with a "Failed to <verb>" toast. Called from
        # the worker thread that did the request — the callback is
        # responsible for routing back to the main thread (typically
        # via GLib.idle_add).
        self._on_unauthorized   = on_unauthorized
        self._unauthorized_seen = False
        # Online/offline state callbacks. Fired only on transitions
        # (offline → online / online → offline) so the UI doesn't get
        # spammed with redundant updates from a busy request loop.
        # Both run on the worker thread that did the request — same
        # idle_add convention as on_unauthorized.
        self._on_online    = on_online
        self._on_offline   = on_offline
        self._currently_online = True   # assumed-online at startup

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

        # Retry transient failures (network errors and 502/503/504) for
        # GET only. Writes typically use source_guid for server-side
        # dedup but a few endpoints (likes, pins) don't, so we don't
        # gamble on retrying mutations.
        # 10s per attempt — long enough for a slow mobile connection,
        # short enough that a network outage surfaces in the offline
        # banner within ~10 s instead of the urllib default 30 s.
        retryable = (method == "GET")
        last_exc  = None
        for attempt in range(len(self.RETRY_BACKOFF_S) + 1):
            try:
                with urllib.request.urlopen(req, body, timeout=10) as r:
                    raw = r.read().decode()
                    dbg("← %d  %d bytes", r.status, len(raw))
                    # DELETE and some other endpoints return 204 No Content
                    # with an empty body — synthesize a meta wrapper so
                    # callers can use the usual _ok() / response shape.
                    self._fire_online()
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
                # We got a response — server is reachable, so we're
                # online. Then decide whether to retry by code.
                self._fire_online()
                if (retryable and e.code in self.RETRY_HTTP_CODES
                        and attempt < len(self.RETRY_BACKOFF_S)):
                    dbg("← HTTP %d  retrying in %ss",
                        e.code, self.RETRY_BACKOFF_S[attempt])
                    time.sleep(self.RETRY_BACKOFF_S[attempt])
                    continue
                raw = ""
                try:
                    raw = e.read().decode()
                    parsed = json.loads(raw)
                    dbg("← HTTP %d  body: %s", e.code, raw[:400])
                    self._maybe_fire_unauthorized(e.code)
                    return parsed
                except Exception:
                    dbg("← HTTP %d  (non-JSON body: %s)", e.code, raw[:200])
                    self._maybe_fire_unauthorized(e.code)
                    return {"meta": {"code": e.code, "errors": [str(e)]}}
            except Exception as e:
                last_exc = e
                dbg("← EXCEPTION: %s", e)
                # Surface offline immediately on the first failure so
                # the user sees the banner during retries, not only
                # after they exhaust. _fire_offline is idempotent at
                # the transition level, so calling it on each attempt
                # is harmless. If a retry succeeds, _fire_online will
                # hide the banner before _req returns.
                self._fire_offline()
                if retryable and attempt < len(self.RETRY_BACKOFF_S):
                    time.sleep(self.RETRY_BACKOFF_S[attempt])
                    continue
                log.exception("Unexpected error in _req(%s %s)",
                              method, endpoint)
                return {"meta": {"code": 0, "errors": [str(e)]}}
        # Loop exited without returning (only happens if all retry
        # paths fall through — defensive).
        self._fire_offline()
        return {"meta": {"code": 0, "errors": [str(last_exc) if last_exc else "request failed"]}}

    def _fire_online(self):
        """Mark the connection healthy. Fires `on_online` only on the
        offline → online transition so the UI doesn't get spammed."""
        if self._currently_online:
            return
        self._currently_online = True
        cb = self._on_online
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            dbg("on_online callback raised: %s", e)

    def _fire_offline(self):
        """Mark the connection unhealthy after a request fully fails.
        Fires `on_offline` only on the online → offline transition."""
        if not self._currently_online:
            return
        self._currently_online = False
        cb = self._on_offline
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            dbg("on_offline callback raised: %s", e)

    def _maybe_fire_unauthorized(self, code: int):
        """Fire `on_unauthorized` exactly once per session when a token-
        bearing request returns 401. Without the one-shot guard, any
        post-expiry burst of API calls (push reconnect retry, sidebar
        refresh, message poll) would each trigger a separate session-
        expired prompt."""
        if code != 401:
            return
        if not self.token:
            return   # request wasn't authenticated; not our problem
        if self._unauthorized_seen:
            return
        self._unauthorized_seen = True
        cb = self._on_unauthorized
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            dbg("on_unauthorized callback raised: %s", e)

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

    # ── file upload (non-image attachments) ──
    #
    # Recovered from web.groupme.com (2026-04-30). Three steps:
    #   1. POST file.groupme.com/v1/{cid}/files?name=<urlencoded>
    #      — raw bytes, Content-Type set to the file's MIME type.
    #      Server returns a JSON envelope with the file_id (which
    #      doubles as the upload job id).
    #   2. GET .../uploadStatus?job=<file_id>&cnt=N  — poll until
    #      `status == "completed"`. cnt is a sequential 0,1,2…
    #      counter the web client increments per poll.
    #   3. Caller attaches `{type:"file", file_id:<id>}` to a message
    #      body and POSTs it via the standard /groups/{gid}/messages.
    #
    # The metadata (file_name / file_size / mime_type) is fetched
    # separately from the receive side via `get_file_data` — the
    # message attachment itself only carries the id.

    UPLOAD_POLL_INTERVAL_S = 1.0
    UPLOAD_POLL_TIMEOUT_S  = 60   # ~60 polls; web client allows much longer

    def upload_file(self, cid: str, file_path: str):
        """Upload a non-image file. `cid` is the same conversation_id
        used for edit/delete/pin: `group_id` for groups, `<lo>+<hi>`
        for DMs. Returns the file_id on success, or None on any
        failure. Blocking — call from a worker thread."""
        path = Path(file_path)
        name = path.name
        # Best-effort MIME guess; default to octet-stream which the
        # server accepts for arbitrary binaries.
        mime, _ = mimetypes.guess_type(str(path))
        mime    = mime or "application/octet-stream"

        try:
            data = path.read_bytes()
        except Exception as e:
            dbg("upload_file: read failed %s: %s", file_path, e)
            return None

        url = (f"{GROUPME_FILE}/v1/{cid}/files"
               f"?name={urllib.parse.quote(name)}")
        dbg("upload_file: %s  mime=%s  bytes=%d", name, mime, len(data))

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type",     mime)
        req.add_header("X-Access-Token",   self.token or "")
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        req.add_header("User-Agent",       f"GroupMe-GNOME/{APP_VERSION}")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode()
                dbg("upload_file: post→ %d  body=%s",
                    resp.status, raw[:300])
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:200]
            except Exception:
                err_body = ""
            dbg("upload_file: HTTP %d  %s", e.code, err_body)
            return None
        except Exception as e:
            dbg("upload_file: exception %s", e)
            return None

        # Server response shape (recovered): {"status_url": "...", "file_id": "..."}
        # We try each common key, then fall back to parsing job= out of
        # status_url.
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}
        file_id = (payload.get("file_id") or
                   payload.get("job_id")   or "")
        status_url = payload.get("status_url") or ""
        if not file_id and status_url:
            try:
                qs = urllib.parse.parse_qs(
                    urllib.parse.urlparse(status_url).query)
                file_id = (qs.get("job") or [""])[0]
            except Exception:
                file_id = ""
        if not file_id:
            dbg("upload_file: could not extract file_id from %s", payload)
            return None

        # Step 2: poll until completed.
        if not status_url:
            status_url = (f"{GROUPME_FILE}/v1/{cid}/uploadStatus"
                          f"?job={file_id}")

        deadline = time.time() + self.UPLOAD_POLL_TIMEOUT_S
        cnt = 0
        while time.time() < deadline:
            poll_url = f"{status_url}&cnt={cnt}" \
                if "cnt=" not in status_url else status_url
            req = urllib.request.Request(poll_url)
            req.add_header("X-Access-Token",   self.token or "")
            req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    s_raw = r.read().decode()
                s = json.loads(s_raw) if s_raw.strip() else {}
            except Exception as e:
                dbg("upload_file: status poll failed: %s", e)
                s = {}
            status = s.get("status", "")
            dbg("upload_file: poll cnt=%d  status=%s", cnt, status)
            if status == "completed":
                return s.get("file_id") or file_id
            if status in ("failed", "error"):
                return None
            cnt += 1
            time.sleep(self.UPLOAD_POLL_INTERVAL_S)
        dbg("upload_file: timed out waiting for completion")
        return None

    def get_file_data(self, cid: str, file_ids: list):
        """Resolve {file_name, file_size, mime_type} for one or more
        file_ids via POST file.groupme.com/v1/{cid}/fileData. Returns a
        dict mapping file_id → file_data dict. Empty dict on failure."""
        if not file_ids:
            return {}
        url  = f"{GROUPME_FILE}/v1/{cid}/fileData"
        body = json.dumps({"file_ids": list(file_ids)}).encode("utf-8")
        req  = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type",     "application/json")
        req.add_header("X-Access-Token",   self.token or "")
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode()
            entries = json.loads(raw) if raw.strip() else []
        except Exception as e:
            dbg("get_file_data: failed: %s", e)
            return {}
        out = {}
        for ent in entries or []:
            fid = ent.get("file_id")
            fd  = ent.get("file_data") or {}
            if fid and fd:
                out[fid] = fd
        return out

    def file_download_url(self, cid: str, file_id: str) -> str:
        """Build a download URL for a file attachment.

        Verified against web.groupme.com 2026-04-30. The query param
        is `access_token`, NOT `token` like elsewhere in the API.
        `_dl=<unix_ms>` is a cache buster the web client adds; harmless
        to include and helps bypass any intermediary cache."""
        return (f"{GROUPME_FILE}/v1/{cid}/files/{file_id}"
                f"?access_token={urllib.parse.quote(self.token or '')}"
                f"&_dl={int(time.time() * 1000)}")

    def download_file(self, cid: str, file_id: str, dest_path: str) -> bool:
        """Stream an authenticated file attachment to `dest_path`.
        Returns True on success.

        Streams in 64 KB chunks so large attachments don't fully
        buffer in memory. The request carries the token in both the
        query string (via file_download_url) and the X-Access-Token
        header — the server accepts either; harmless to send both."""
        url = self.file_download_url(cid, file_id)
        dbg("download_file: %s → %s", file_id, dest_path)
        req = urllib.request.Request(url)
        req.add_header("X-Access-Token",   self.token or "")
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        req.add_header("User-Agent",       f"GroupMe-GNOME/{APP_VERSION}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, \
                 open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            dbg("download_file: ok")
            return True
        except urllib.error.HTTPError as e:
            dbg("download_file: HTTP %d", e.code)
            return False
        except Exception as e:
            dbg("download_file: exception %s", e)
            return False


# ─────────────────────────── Faye Push Client ────────────────────

