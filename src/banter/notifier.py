"""Banter — headless notifier for `banter --background`.

Owns Config + GroupMeAPI + GroupMePush without building any UI. The
intended deployment is an autostart entry that launches the app at
login so we can deliver desktop notifications even before the user
opens the main window.

Notification dispatch is shared with BanterWindow via
NotificationDispatcher; everything else here is a stripped-down
counterpart to the window's _start_push / _process_bg_update /
_handle_*_push_message paths, minus the row/list reconciliation.

Persisted state: Config.{get,set}_last_seen_map keep a per-account
conv_key → message_id watermark across runs, so a reboot doesn't
reset the "new since I last looked" boundary.
"""

from gi.repository import GLib

from .api import GroupMeAPI
from .async_utils import run_in_background
from .constants import DEMO
from .helpers import is_hidden_system_message, format_preview
from .notifications import NotificationDispatcher
from .push import GroupMePush

import logging

log = logging.getLogger(__name__)


class BanterNotifier:
    def __init__(self, app):
        self._app           = app
        self._config        = app.config
        self._notifications = NotificationDispatcher(app, self._config)
        self._api           = None
        self._push          = None
        self._user          = None
        # conv_key ("group:<gid>" / "dm:<other_id>") → last seen msg id.
        # Loaded on _on_verified, written through to disk on every change.
        self._last_seen     : dict = {}
        # Cache of group_id → display name, populated by the catch-up
        # /groups fetch and used to title push-event notifications
        # (push payloads carry no group name). DM titles come straight
        # from the sender's `name` field on the message.
        self._group_names   : dict = {}

    # ── lifecycle ──────────────────────────────────────────────────
    def start(self) -> bool:
        """Bootstrap the notifier. Returns True if we're going to run
        (caller should `app.hold()`); False if there's nothing to do
        (no signed-in account or DEMO mode)."""
        if DEMO:
            return False
        acc = self._config.get_active_account()
        if not acc or not acc.get("token"):
            log.info("banter --background: no active account, exiting")
            return False
        self._api = GroupMeAPI(acc["token"],
                                on_unauthorized=self._on_session_expired)

        def verify():
            try:
                me = self._api.get_me()
            except Exception as e:
                log.debug("notifier: /users/me failed: %s", e)
                return
            GLib.idle_add(self._on_verified, me)

        run_in_background(verify)
        return True

    def stop(self):
        if self._push:
            self._push.stop()
            self._push = None

    def _on_session_expired(self):
        # Fired by GroupMeAPI on 401 from a worker thread. Headless mode
        # has no sign-in surface to re-prompt, so tear everything down
        # and release the app's hold; the user can re-auth next time
        # they open the window.
        GLib.idle_add(self._handle_session_expired)

    def _handle_session_expired(self):
        # Headless mode is the only thing holding the app alive, so
        # quitting drops the hold cleanly; the user can re-auth next
        # time they launch banter.
        log.info("notifier: token rejected (401), stopping background daemon")
        self.stop()
        self._app.quit()
        return False

    def _on_verified(self, me: dict):
        if not me:
            return
        self._user = me
        self._last_seen = self._config.get_last_seen_map(str(me.get("id", "")))
        self._start_push()
        self._catch_up()

    # ── push ───────────────────────────────────────────────────────
    def _start_push(self):
        if self._push or not (self._api and self._user):
            return
        self._push = GroupMePush(
            self._api.token, str(self._user["id"]),
            on_event=self._on_push_event,
            on_error=lambda m: log.debug("push error: %s", m))
        self._push.start()
        log.debug("notifier: push started for user %s", self._user.get("id"))

    def _on_push_event(self, data: dict):
        ev_type = data.get("type", "")
        if ev_type not in ("line.create", "direct_message.create"):
            return
        subject = data.get("subject", {}) or {}
        if is_hidden_system_message(subject):
            return

        gid = str(subject.get("group_id", ""))
        if gid:
            self._handle_group_push(gid, subject)
        else:
            self._handle_dm_push(subject)

    def _handle_group_push(self, gid: str, subject: dict):
        key = f"group:{gid}"
        msg_id = str(subject.get("id", ""))
        if msg_id:
            self._last_seen[key] = msg_id

        # Self-echo filter. Group push payloads carry the sender's
        # nickname but no user_id, so we compare against our own
        # display name (same heuristic the window uses).
        sender = subject.get("name", "")
        my_name = (self._user or {}).get("name", "")
        if my_name and sender == my_name:
            self._save_last_seen()
            return

        title = self._group_names.get(gid) or "GroupMe"
        if self._notifications.handle_call_event(
                "group", gid, subject, conv_title=title):
            self._save_last_seen()
            return

        preview_text = format_preview(subject.get("text"),
                                      subject.get("attachments"))
        body = (f"{sender}: {preview_text or '📎 attachment'}"
                if sender else (preview_text or "📎 attachment"))
        self._notifications.send(title, body, tag=f"group-{gid}")
        self._save_last_seen()

    def _handle_dm_push(self, subject: dict):
        sender_uid = str(subject.get("user_id", "")
                         or subject.get("sender_id", ""))
        me_id = str((self._user or {}).get("id", ""))
        if not sender_uid or sender_uid == me_id:
            return   # self-echo
        other_id = sender_uid
        key = f"dm:{other_id}"
        msg_id = str(subject.get("id", ""))
        if msg_id:
            self._last_seen[key] = msg_id

        sender = subject.get("name") or "Someone"
        if self._notifications.handle_call_event(
                "dm", other_id, subject, conv_title=sender):
            self._save_last_seen()
            return

        preview_text = format_preview(subject.get("text"),
                                      subject.get("attachments"))
        self._notifications.send(
            sender, preview_text or "📎 attachment",
            tag=f"dm-{other_id}")
        self._save_last_seen()

    # ── REST catch-up ──────────────────────────────────────────────
    def _catch_up(self):
        """One-shot REST sweep run right after push starts.

        Fires notifications for any conversation whose server-side
        last_message_id has advanced past the persisted watermark.
        First-ever run (empty watermark map) just seeds without
        notifying — otherwise we'd flood the user with everything in
        their backlog the first time they enable autostart."""
        def worker():
            try:
                groups = self._api.get_groups_all()
                chats  = self._api.get_chats_all()
            except Exception as e:
                log.debug("notifier: catch-up fetch failed: %s", e)
                return
            GLib.idle_add(self._process_catch_up, groups, chats)

        run_in_background(worker)

    def _process_catch_up(self, groups, chats):
        first_run = not self._last_seen
        me_id   = str((self._user or {}).get("id", ""))
        my_name = (self._user or {}).get("name", "")

        # Cache group names for push-event notifications first; this is
        # cheap and useful even on first-run (where we skip notifying).
        for g in groups:
            self._group_names[str(g["id"])] = g.get("name", "GroupMe")

        for g in groups:
            gid = str(g["id"])
            key = f"group:{gid}"
            msgs = g.get("messages") or {}
            last_id = msgs.get("last_message_id")
            if not last_id:
                continue
            prev_id = self._last_seen.get(key)
            if last_id == prev_id:
                continue
            self._last_seen[key] = last_id
            if first_run:
                continue
            preview = msgs.get("preview", {}) or {}
            sender = preview.get("nickname", "")
            if my_name and sender == my_name:
                continue
            preview_text = format_preview(preview.get("text"),
                                          preview.get("attachments"))
            body = (f"{sender}: {preview_text or '📎 attachment'}"
                    if sender else (preview_text or "📎 attachment"))
            self._notifications.send(
                g.get("name", "GroupMe"), body, tag=f"group-{gid}")

        for chat in chats:
            other = chat.get("other_user", {}) or {}
            other_id = str(other.get("id", ""))
            if not other_id:
                continue
            key = f"dm:{other_id}"
            lm = chat.get("last_message", {}) or {}
            last_id = lm.get("id")
            if not last_id:
                continue
            prev_id = self._last_seen.get(key)
            if last_id == prev_id:
                continue
            self._last_seen[key] = last_id
            if first_run:
                continue
            sender_id = str(lm.get("sender_id") or lm.get("user_id") or "")
            if me_id and sender_id == me_id:
                continue
            preview_text = format_preview(lm.get("text"), lm.get("attachments"))
            self._notifications.send(
                other.get("name", "Someone"),
                preview_text or "📎 attachment",
                tag=f"dm-{other_id}")

        self._save_last_seen()

    # ── persistence ────────────────────────────────────────────────
    def _save_last_seen(self):
        if not self._user:
            return
        self._config.set_last_seen_map(
            str(self._user.get("id", "")), self._last_seen)
