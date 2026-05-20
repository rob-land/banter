"""PinnedMixin — pinned-message id set + jump-to-message helper.

Mixed into ChatView. Tracks the set of pinned-message ids for the
current conversation; the MessageBubble's pin indicator reads from
this via `is_pinned(msg_id)`. `mark_pinned` updates after a local
pin/unpin action; `_fetch_pinned` re-syncs from the server.
"""

from gi.repository import GLib

from ...async_utils import run_in_background


class PinnedMixin:
    def _fetch_pinned(self):
        """Refresh `_pinned_ids` from the server and update any bubbles
        that are already on screen."""
        is_dm     = self._is_dm
        other_uid = self._other_uid
        gid       = self._gid

        def worker():
            if is_dm:
                msgs = self._api.get_pinned_dm(other_uid)
            else:
                msgs = self._api.get_pinned_group(gid)
            ids = {str(m.get("id")) for m in (msgs or []) if m.get("id")}
            GLib.idle_add(self._on_pinned_loaded, ids, msgs or [])

        run_in_background(worker)

    def _on_pinned_loaded(self, ids: set, _msgs: list):
        self._pinned_ids = ids
        # Re-render the indicator on any bubble whose pin state may have
        # flipped. We recompute for every loaded bubble — cheap and
        # avoids tracking diffs across fetches.
        for mid, bubble in list(self._bubble_map.items()):
            try:
                bubble.set_pinned(mid in ids)
            except Exception:
                pass

    def is_pinned(self, msg_id) -> bool:
        return str(msg_id) in self._pinned_ids

    def mark_pinned(self, msg_id, pinned: bool):
        """Update `_pinned_ids` and the bubble indicator after a local
        pin/unpin action. Called by MessageBubble on success."""
        mid = str(msg_id)
        if pinned:
            self._pinned_ids.add(mid)
        else:
            self._pinned_ids.discard(mid)
        bubble = self._bubble_map.get(mid)
        if bubble is not None:
            try:
                bubble.set_pinned(pinned)
            except Exception:
                pass

    def jump_to_message(self, msg_id):
        """Scroll the existing bubble for `msg_id` into view. If it isn't
        in `_bubble_map` (e.g. the user hasn't loaded that far back), no-op
        and return False so the caller can show a toast."""
        bubble = self._bubble_map.get(str(msg_id))
        if bubble is None:
            return False
        self._scroll_to_bubble(bubble)
        return True
