"""ReadReceiptsMixin — send our read pointer + watch the other DM
participant's pointer to stamp ✓ on our own bubbles.

Mixed into ChatView. Both directions are idempotent / throttled:
outbound only fires when `_newest_id` advances past `_last_receipt_id`
AND the user is at the bottom; inbound only walks the bubble map when
the other user's pointer changes.
"""

from gi.repository import GLib

from ...async_utils import run_in_background


class ReadReceiptsMixin:
    def _send_read_receipt(self):
        """Mark the conversation read up to `_newest_id` if we haven't
        already done so.

        Idempotent + throttled by `_last_receipt_id`: repeated calls
        with the same newest message no-op. Skipped when the user is
        scrolled away from the bottom — they haven't actually seen
        the latest content, and silently advancing the read pointer
        would tell other clients we *had*. Fired from initial load,
        push line.create while at-bottom, the DM poll path, and on
        scroll-back-to-bottom."""
        target = self._newest_id
        if not target:
            return
        if self._last_receipt_id == target:
            return
        if not self._at_bottom:
            return
        cid = self._conv_id()
        api = self._api
        # Optimistically advance the throttle marker before the network
        # round-trip — prevents back-to-back fires (e.g. push event
        # arriving during initial load) from each issuing a duplicate
        # POST. Reverted on failure.
        prev = self._last_receipt_id
        self._last_receipt_id = target

        def worker():
            ok = api.read_receipt(cid) is not None
            if not ok:
                # Roll back so the next trigger retries.
                GLib.idle_add(self._on_receipt_failed, prev)
        run_in_background(worker)

    def _on_receipt_failed(self, prev_id):
        # Only roll back if nothing else advanced the marker in the
        # meantime — otherwise a successful follow-up receipt would
        # get clobbered by this rollback.
        if self._last_receipt_id and self._last_receipt_id == self._newest_id:
            self._last_receipt_id = prev_id

    # DMs only: poll for the other user's read pointer and, if it
    # advanced, refresh the Read indicator on our own bubbles. Cheap
    # one-message GET (`limit=1`) hung off the existing _poll tick
    # and the initial-load completion.
    def _refresh_dm_read_receipt(self):
        if not self._is_dm:
            return
        other_uid = self._other_uid
        api       = self._api

        def worker():
            receipt = api.get_dm_read_receipt(other_uid)
            GLib.idle_add(self._on_dm_read_receipt, receipt)

        run_in_background(worker)

    def _on_dm_read_receipt(self, receipt):
        if not isinstance(receipt, dict):
            return
        mid = str(receipt.get("message_id") or "")
        rat = int(receipt.get("read_at") or 0)
        if not mid:
            return
        # Skip the per-bubble walk if the pointer hasn't moved.
        if mid == self._other_read_id and rat == self._other_read_at:
            return
        self._other_read_id = mid
        self._other_read_at = rat
        self._apply_dm_read_receipt()

    @staticmethod
    def _id_le(a, b) -> bool:
        """`a <= b` for GroupMe message ids. The ids are 18-digit
        timestamp-derived integers; lexicographic compare happens to
        agree, but int compare is the documented stable order so use
        that and fall back to lex on parse failure."""
        try:
            return int(a) <= int(b)
        except (TypeError, ValueError):
            return str(a) <= str(b)

    def _apply_dm_read_receipt(self):
        """Walk own bubbles and toggle the Read indicator based on the
        latest known `_other_read_id`. Bubbles whose id <= the read
        pointer get the ✓; everything past it gets cleared (handles
        the rare case where the pointer somehow regressed)."""
        if not self._is_dm or not self._other_read_id:
            return
        ptr = self._other_read_id
        rat = self._other_read_at
        for mid, bubble in self._bubble_map.items():
            try:
                if not getattr(bubble, "is_mine", False):
                    continue
                if self._id_le(mid, ptr):
                    bubble.set_read(rat)
                else:
                    bubble.set_read(None)
            except Exception:
                # Bubble may have been removed or torn down; skip.
                continue
