"""TypingMixin — outbound throttled typing pulses + inbound indicator bar.

Mixed into ChatView. Reads/writes instance state set up in the host
class's `__init__`: `self._typing_users`, `self._typing_clear_id`,
`self._last_typing_sent`, `self._typing_bar`, `self._typing_lbl`,
`self._entry`, `self._win`, `self._is_dm`, `self._me`, `self._gid`,
`self._other_uid`.

Class constants `TYPING_PULSE_INTERVAL` and `TYPING_DECAY_SECS` live
on ChatView itself; the host inherits this mixin so the methods
resolve them via MRO.
"""

import time

from gi.repository import GLib


class TypingMixin:
    def _dm_channel_key(self) -> str:
        """Underscore-joined sorted-user-id pair used as the suffix of
        the /direct_message/<key> Faye channel for this DM. Mirrors
        the HTTP conversation_id format (`<lo>+<hi>`) but with `_` as
        the separator — Faye channel names disallow `+`."""
        try:
            a, b = int(self._me), int(self._other_uid)
            lo, hi = (a, b) if a < b else (b, a)
            return f"{lo}_{hi}"
        except (TypeError, ValueError):
            return f"{self._me}_{self._other_uid}"

    def _on_buf_changed_typing(self, _buf):
        """Throttled outbound typing pulse. Pulses are best-effort — the
        push client silently drops them while reconnecting.
        Routes to /group/{gid} for groups or /direct_message/<key> for
        DMs based on conversation type."""
        buf  = self._entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(),
                              buf.get_end_iter(), False)
        # Don't pulse if compose is empty (deleting the last char
        # shouldn't tell anyone we're "typing").
        if not text.strip():
            return
        now = time.monotonic()
        if (now - self._last_typing_sent) < self.TYPING_PULSE_INTERVAL:
            return
        push = getattr(self._win, "_push", None)
        if push is None:
            return
        if self._is_dm:
            ok = push.publish_typing_dm(self._dm_channel_key())
        else:
            ok = push.publish_typing_group(self._gid)
        if ok:
            self._last_typing_sent = now

    def _on_typing_received(self, uid: str):
        """Record an incoming typing pulse from `uid` and refresh the bar."""
        self._typing_users[uid] = time.monotonic() + self.TYPING_DECAY_SECS
        self._refresh_typing_indicator()
        # Re-arm a single timer so the bar self-clears even if no further
        # pulses arrive. Using a fresh timeout per pulse is overkill —
        # one timer that re-checks the dict on fire is enough.
        if self._typing_clear_id == 0:
            self._typing_clear_id = GLib.timeout_add(
                int(self.TYPING_DECAY_SECS * 1000) + 200,
                self._on_typing_decay_tick)

    def _on_typing_decay_tick(self):
        now = time.monotonic()
        expired = [u for u, deadline in self._typing_users.items()
                   if deadline <= now]
        for u in expired:
            self._typing_users.pop(u, None)
        self._refresh_typing_indicator()
        if not self._typing_users:
            self._typing_clear_id = 0
            return False
        return True   # keep ticking until the dict drains

    def _refresh_typing_indicator(self):
        """Update the visible 'X is typing…' label."""
        users = list(self._typing_users.keys())
        if not users:
            self._typing_bar.set_visible(False)
            self._typing_lbl.set_text("")
            return
        names = [self._win.get_user_name(u) for u in users]
        if len(names) == 1:
            text = f"{names[0]} is typing…"
        elif len(names) == 2:
            text = f"{names[0]} and {names[1]} are typing…"
        else:
            text = "Several people are typing…"
        self._typing_lbl.set_text(text)
        self._typing_bar.set_visible(True)
