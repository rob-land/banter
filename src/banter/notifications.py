"""Banter — desktop notification dispatcher.

Pure notification I/O, factored out so both BanterWindow and the future
headless `--background` notifier can share the mute-key resolution and
call-event rendering without forking the logic.

The dispatcher holds no UI state. Callers that need conversation titles
for call-event notifications pass them in directly rather than relying
on a sidebar row lookup.
"""

from gi.repository import Gio

from .constants import dbg


class NotificationDispatcher:
    def __init__(self, app, config):
        self._app    = app
        self._config = config

    def send(self, title: str, body: str,
             tag: str = "banter-msg",
             buttons: list = None,
             mute_key: str = None):
        """Send a desktop notification via GApplication (works in Flatpak).

        `buttons` is an optional list of (label, detailed_action) pairs
        passed straight to `Gio.Notification.add_button` — used by the
        call-started notification to attach a "Join" action.

        `mute_key` lets the caller specify the mute lookup key directly
        when the tag doesn't follow the `group-` / `dm-` convention
        (e.g. call notifications use `call-group-<gid>` to avoid being
        clobbered by an unrelated group message). When omitted we fall
        back to deriving the key from the tag prefix."""
        if mute_key is None:
            if tag.startswith("group-"):
                mute_key = tag[len("group-"):]
            elif tag.startswith("dm-"):
                mute_key = "dm:" + tag[len("dm-"):]
        if mute_key and self._config.is_muted(mute_key):
            dbg("notification suppressed (muted): %s", tag)
            return
        try:
            if self._app is None:
                return
            notif = Gio.Notification.new(title)
            notif.set_body(body[:200])
            notif.set_icon(Gio.ThemedIcon.new("land.rob.Banter"))
            notif.set_priority(Gio.NotificationPriority.HIGH)
            notif.set_default_action("app.activate")
            for label, detailed in (buttons or ()):
                notif.add_button(label, detailed)
            self._app.send_notification(tag, notif)
            dbg("notification sent: [%s] %s – %s", tag, title, body[:60])
        except Exception as e:
            dbg("notification error: %s", e)

    def withdraw(self, tag: str):
        try:
            if self._app:
                self._app.withdraw_notification(tag)
        except Exception as e:
            dbg("notification withdraw failed (%s): %s", tag, e)

    def handle_call_event(self, conv_type: str, conv_id: str,
                          subject: dict, conv_title: str = "") -> bool:
        """Fire a call-specific notification with a Join button when a
        Faye `group.call.started` system message arrives, or withdraw
        the prior start notification on `group.call.ended`. Returns
        True iff the subject was a call event and was handled — caller
        should skip its generic notification path in that case.

        `conv_title` is shown as the notification title; pass the
        group's name or the DM peer's display name. Falls back to
        "GroupMe call" when blank.

        Unlike regular messages we surface this even when the chat is
        open, since the Join action is actionable from the OS notification
        layer too (and a call is a higher-signal event than a text)."""
        event = subject.get("event") or {}
        et = event.get("type", "")
        tag = f"call-{conv_type}-{conv_id}"
        mute_key = (conv_id if conv_type == "group" else f"dm:{conv_id}")

        if et == "group.call.ended":
            self.withdraw(tag)
            return True

        if et != "group.call.started":
            return False

        data = event.get("data") or {}
        starter = ((data.get("user") or {}).get("nickname")
                   or "Someone")
        title = conv_title or "GroupMe call"
        body = f"📞 {starter} started a call"
        # Detailed-action form passes the conv_id as the string param.
        # Group ids and DM <lo>+<hi> conv ids are both safe in this
        # encoding (digits/+/-).
        join_action = f"app.call-join::{conv_id}"
        self.send(
            title, body, tag=tag,
            buttons=[("Join", join_action)],
            mute_key=mute_key)
        return True
