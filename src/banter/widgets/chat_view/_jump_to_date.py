"""JumpToDateMixin — calendar-based history navigation + load-older.

Mixed into ChatView. Implements the "jump to date" dialog flow:
walk backward through the message history in batches until a target
date is reached, then prepend the whole window in one shot and scroll
to the first bubble on that date.
"""

from datetime import datetime

from gi.repository import GLib

from ...async_utils import run_in_background


class JumpToDateMixin:
    # 100 is the largest limit GroupMe accepts; the web client uses it.
    # 100 × 100 = 10k messages of backfill, which covers many months of
    # even a very active group. If the target is still further back the
    # user gets a clear toast and can re-trigger to continue.
    JUMP_BATCH_SIZE = 100
    JUMP_MAX_BATCHES = 100

    def jump_to_date(self, target):
        """Scroll the conversation back to messages from `target` (a
        datetime.date).

        If the target date isn't already loaded, page backward through
        history in JUMP_BATCH_SIZE-message batches until the oldest
        message in a batch is on or before `target`, then prepend the
        whole window in one shot and scroll to the first bubble that
        falls on or after the target date. Bounded by JUMP_MAX_BATCHES
        so a runaway worker can't loop indefinitely."""
        if self._loading:
            self._win.toast("Already loading messages — try again in a sec")
            return

        # If a bubble for that day is already loaded, just scroll.
        bubble = self._find_bubble_at_or_after(target)
        if bubble is not None and self._is_bubble_on_date(bubble, target):
            self._scroll_to_bubble(bubble)
            return

        # Otherwise page backward.
        self._loading = True
        try:
            self._win.toast(f"Loading messages from "
                             f"{target.strftime('%b %-d, %Y')}…")
        except Exception:
            pass

        is_dm     = self._is_dm
        other_uid = self._other_uid
        gid       = self._gid
        cur_oldest = self._oldest_id
        target_unix = int(datetime.combine(
            target, datetime.min.time()).timestamp())

        def worker():
            collected = []   # newest-first, accumulated across batches
            found_target = False
            exhausted    = False
            for _ in range(self.JUMP_MAX_BATCHES):
                before_id = (collected[-1]["id"] if collected
                             else cur_oldest)
                if not before_id:
                    exhausted = True
                    break
                if is_dm:
                    msgs = self._api.get_dm_messages(
                        other_uid, before_id=before_id,
                        limit=self.JUMP_BATCH_SIZE)
                else:
                    msgs = self._api.get_messages(
                        gid, before_id=before_id,
                        limit=self.JUMP_BATCH_SIZE)
                if not msgs:
                    exhausted = True
                    break
                collected.extend(msgs)
                if int(msgs[-1].get("created_at", 0)) <= target_unix:
                    found_target = True
                    break
            GLib.idle_add(self._on_jump_loaded, collected, target,
                          found_target, exhausted)

        run_in_background(worker)

    def _on_jump_loaded(self, msgs, target, found_target, exhausted):
        # _prepend_old expects newest-first, sets _loading=False at the
        # top, and takes care of date-separator boundaries.
        self._prepend_old(msgs)
        bubble = self._find_bubble_at_or_after(target)
        if bubble is not None:
            self._scroll_to_bubble(bubble)

        # Tell the user what actually happened — silent jumps that land
        # weeks short of the requested date are confusing.
        if found_target:
            return   # target reached; no toast needed
        if exhausted and msgs:
            oldest_dt = datetime.fromtimestamp(
                int(msgs[-1].get("created_at", 0))).date()
            try:
                self._win.toast(
                    f"Reached start of conversation at "
                    f"{oldest_dt.strftime('%b %-d, %Y')}")
            except Exception:
                pass
        elif exhausted and not msgs:
            try:
                self._win.toast("No older messages found")
            except Exception:
                pass
        else:
            # Hit the batch cap without reaching the date. Tell the
            # user where we got to so they know to jump again.
            oldest_dt = datetime.fromtimestamp(
                int(msgs[-1].get("created_at", 0))).date() if msgs else target
            try:
                self._win.toast(
                    f"Loaded back to {oldest_dt.strftime('%b %-d, %Y')} — "
                    f"jump again to keep going")
            except Exception:
                pass

    def _is_bubble_on_date(self, bubble, target) -> bool:
        try:
            ts = int(bubble.msg.get("created_at", 0))
        except (TypeError, ValueError):
            return False
        return datetime.fromtimestamp(ts).date() == target

    def _find_bubble_at_or_after(self, target):
        """Return the loaded bubble with the earliest created_at that is
        on or after `target` (a date), or None."""
        target_unix = int(datetime.combine(
            target, datetime.min.time()).timestamp())
        best = None
        best_ts = None
        for bubble in self._bubble_map.values():
            try:
                ts = int(bubble.msg.get("created_at", 0))
            except (TypeError, ValueError):
                continue
            if ts >= target_unix and (best_ts is None or ts < best_ts):
                best = bubble
                best_ts = ts
        return best

    def _load_more(self, *_):
        if self._loading or not self._oldest_id:
            return
        self._loading = True

        def worker():
            if self._is_dm:
                msgs = self._api.get_dm_messages(
                    self._other_uid, before_id=self._oldest_id, limit=20)
            else:
                msgs = self._api.get_messages(
                    self._gid, before_id=self._oldest_id, limit=20)
            GLib.idle_add(self._prepend_old, msgs)

        run_in_background(worker)
