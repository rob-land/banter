"""Banter — GroupMe REST API client: base + connectivity + auth.

The bulk of the API surface lives in the feature mixins
(`groups.py`, `dms.py`, `media.py`, `extras.py`); this module owns
the constructor, the low-level `_req` HTTP helper, connectivity
callbacks (`on_online` / `on_offline` / `on_unauthorized`), and the
small auth/me surface that's tightly coupled to the token state.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from ..constants import APP_VERSION, DEBUG, GROUPME_API

log = logging.getLogger(__name__)


class _APIBase:
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
            log.debug("→ %s %s", method, safe_url)
            if data is not None:
                log.debug("  body: %s", json.dumps(data, separators=(",", ":")))

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
                    log.debug("← %d  %d bytes", r.status, len(raw))
                    # DELETE and some other endpoints return 204 No Content
                    # with an empty body — synthesize a meta wrapper so
                    # callers can use the usual _ok() / response shape.
                    self._fire_online()
                    if not raw.strip():
                        return {"meta": {"code": r.status}, "response": None}
                    parsed = json.loads(raw)
                    if DEBUG:
                        code = parsed.get("meta", {}).get("code", "?")
                        log.debug("  meta.code=%s", code)
                        if parsed.get("meta", {}).get("errors"):
                            log.debug("  errors: %s",
                                parsed["meta"]["errors"])
                    return parsed
            except urllib.error.HTTPError as e:
                # We got a response — server is reachable, so we're
                # online. Then decide whether to retry by code.
                self._fire_online()
                if (retryable and e.code in self.RETRY_HTTP_CODES
                        and attempt < len(self.RETRY_BACKOFF_S)):
                    log.debug("← HTTP %d  retrying in %ss",
                        e.code, self.RETRY_BACKOFF_S[attempt])
                    time.sleep(self.RETRY_BACKOFF_S[attempt])
                    continue
                raw = ""
                try:
                    raw = e.read().decode()
                    parsed = json.loads(raw)
                    log.debug("← HTTP %d  body: %s", e.code, raw[:400])
                    self._maybe_fire_unauthorized(e.code)
                    return parsed
                except Exception:
                    log.debug("← HTTP %d  (non-JSON body: %s)", e.code, raw[:200])
                    self._maybe_fire_unauthorized(e.code)
                    return {"meta": {"code": e.code, "errors": [str(e)]}}
            except Exception as e:
                last_exc = e
                log.debug("← EXCEPTION: %s", e)
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
            log.debug("on_online callback raised: %s", e)

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
            log.debug("on_offline callback raised: %s", e)

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
            log.debug("on_unauthorized callback raised: %s", e)

    def _ok(self, r):
        return r.get("meta", {}).get("code") in (200, 201, 204)

    # ── auth / user ──
    def verify_token(self, token: str):
        """Validate an access token by calling /users/me.
        Returns (True, user_dict) on success, (False, [error_strings]) on failure."""
        self.token = token.strip()
        log.debug("verify_token: calling /users/me")
        r = self._req("GET", "/users/me")
        if self._ok(r) and r.get("response"):
            user = r["response"]
            log.debug("verify_token: success – user_id=%s  name=%s",
                user.get("id"), user.get("name"))
            return True, user
        errors = r.get("meta", {}).get("errors") or [
            f"HTTP {r.get('meta', {}).get('code', '?')} – token invalid or expired"
        ]
        log.debug("verify_token: failed – %s", errors)
        self.token = None
        return False, errors

    def get_me(self):
        r = self._req("GET", "/users/me")
        return r.get("response")

    def update_me(self, **kwargs):
        r = self._req("POST", "/users/update", kwargs)
        return r.get("response")
