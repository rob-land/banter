"""MentionsMixin — @-mention autocomplete + the GroupMe mentions
attachment that flags message recipients.

Mixed into ChatView. The popover (`MentionPopover`) is created lazily
after the member list is fetched; until then `@` keystrokes are
silently ignored.
"""

from gi.repository import Gdk, Gtk

from ...async_utils import run_in_background
from ..mention_popover import EVERYONE_ID, MentionPopover


class MentionsMixin:
    def _fetch_members_for_mentions(self):
        """Background-fetch the full group dict (with members) so the
        @-autocomplete has data to filter. The sidebar list uses
        `omit=memberships` for speed, so the group dict we were
        constructed with is usually member-less."""
        # Prefer the already-cached full group dict from the contacts
        # tab if it's there — saves an HTTP round trip.
        cached = getattr(self._win, "_all_groups_with_members", None) or []
        for g in cached:
            if str(g.get("id", "")) == self._gid and g.get("members"):
                self._group = g
                self._build_mention_popover()
                return

        gid = self._gid

        def worker():
            return self._api.get_group(gid)

        def on_done(full):
            if full and full.get("members"):
                self._group = full
                self._build_mention_popover()

        run_in_background(worker, on_done)

    def _build_mention_popover(self):
        if self._mention_popover is not None:
            return
        members = self._collect_members()
        if not members:
            return
        self._mention_popover = MentionPopover(members)
        self._mention_popover.set_parent(self._entry)
        self._mention_popover.connect(
            "member-selected", self._on_mention_picked)

    def _refresh_mention_members(self):
        """Re-fetch the group's members from the API and patch the
        popover's list in place. Bypasses the contacts-tab cache, which
        is a long-lived snapshot that won't reflect members added after
        Banter started."""
        if self._mention_popover is None:
            return
        gid = self._gid

        def worker():
            return self._api.get_group(gid)

        def on_done(full):
            if not full or not full.get("members"):
                return
            if self._mention_popover is None:
                return
            self._group = full
            self._mention_popover.set_members(self._collect_members())

        run_in_background(worker, on_done)

    def _collect_members(self) -> list:
        """Return [(display_name, user_id), ...] for the autocomplete,
        excluding the current user. Group-only — DMs never call this."""
        out = []
        for m in (self._group.get("members") or []):
            uid  = str(m.get("user_id") or "")
            name = (m.get("nickname") or m.get("name") or "").strip()
            if name and uid and uid != self._me:
                out.append((name, uid))
        return out

    def _on_buf_changed(self, buf):
        """Driver for the @-mention autocomplete. We watch every buffer
        change (insert + delete) to decide whether to open / update /
        close the popover."""
        if self._mention_popover is None:
            return
        if self._in_mention_pick:
            # We're mutating the buffer ourselves — don't second-guess
            # the popover state mid-replacement.
            return

        cursor_iter = buf.get_iter_at_mark(buf.get_insert())

        # If a popover is already open, update its filter from the text
        # between the @-anchor and the cursor — or close on disqualify.
        if self._mention_anchor is not None:
            anchor_iter = buf.get_iter_at_mark(self._mention_anchor)
            if cursor_iter.get_offset() <= anchor_iter.get_offset():
                # User backspaced through (or before) the @
                self._close_mention_popover()
                return
            after_at = anchor_iter.copy()
            after_at.forward_char()
            prefix = buf.get_text(after_at, cursor_iter, False)
            if any(c.isspace() for c in prefix):
                # Whitespace ends the candidate; abandon
                self._close_mention_popover()
                return
            self._mention_popover.set_filter(prefix)
            if not self._mention_popover.has_results():
                self._close_mention_popover()
            return

        # No active popover — see whether the user just typed a fresh @
        if cursor_iter.get_offset() == 0:
            return
        prev = cursor_iter.copy()
        prev.backward_char()
        if prev.get_char() != "@":
            return
        # Skip mid-word @, e.g. "user@example.com"
        if prev.get_offset() > 0:
            before = prev.copy()
            before.backward_char()
            ch = before.get_char()
            if ch.isalnum() or ch == "_":
                return
        self._open_mention_popover(prev)

    def _open_mention_popover(self, at_iter):
        buf = self._entry.get_buffer()
        # left-gravity mark: stays put even as text is typed after it
        self._mention_anchor = buf.create_mark(None, at_iter, True)

        # Aim the popover at the @ character so it visibly anchors to
        # what the user is typing, instead of floating mid-entry.
        try:
            ir = self._entry.get_iter_location(at_iter)  # buffer coords
            wx, wy = self._entry.buffer_to_window_coords(
                Gtk.TextWindowType.WIDGET, ir.x, ir.y)
            rect = Gdk.Rectangle()
            rect.x = int(wx)
            rect.y = int(wy)
            rect.width  = max(1, ir.width)
            rect.height = max(1, ir.height)
            self._mention_popover.set_pointing_to(rect)
        except Exception:
            # Fall back silently — popover will still appear over the
            # entry, just not pinpoint-aligned.
            pass

        self._mention_popover.set_filter("")
        self._mention_popover.popup()
        # Refresh the member list in the background so users added to
        # the group since this chat was opened (or since the contacts
        # tab cached its snapshot) show up. The popover stays usable
        # against the cached list while the fetch is in flight.
        self._refresh_mention_members()

    def _close_mention_popover(self):
        if self._mention_anchor is not None:
            buf = self._entry.get_buffer()
            buf.delete_mark(self._mention_anchor)
            self._mention_anchor = None
        if self._mention_popover is not None:
            self._mention_popover.popdown()

    def _on_mention_picked(self, _popover, display_name: str, user_id: str):
        """Replace the in-progress `@prefix` with `@<display_name> ` and
        record the bracketing TextMarks so we can recover offsets at
        send time."""
        if self._mention_anchor is None:
            return
        buf = self._entry.get_buffer()

        # Suppress _on_buf_changed for the duration of this method —
        # otherwise the buf.delete below would be misread as the user
        # backspacing through the @ and the popover state would tear
        # itself down before we finish using it.
        self._in_mention_pick = True
        try:
            anchor_iter = buf.get_iter_at_mark(self._mention_anchor)
            cursor_iter = buf.get_iter_at_mark(buf.get_insert())
            buf.delete(anchor_iter, cursor_iter)

            # Iters were invalidated by the delete — re-fetch from the mark.
            anchor_iter = buf.get_iter_at_mark(self._mention_anchor)
            insert_offset = anchor_iter.get_offset()
            inserted = f"@{display_name}"
            buf.insert(anchor_iter, inserted)

            if user_id != EVERYONE_ID:
                # Bracket the inserted span with marks.
                # Gravity matters here: we want both marks to STAY at the
                # original mention boundaries no matter where the user
                # types next.
                #   start: right-gravity (left_gravity=False) → text
                #          inserted at the mention's start position is
                #          pushed BEFORE the mark, mark stays at @.
                #   end:   left-gravity  (left_gravity=True)  → text
                #          inserted at the mention's end position is
                #          pushed AFTER the mark, mark stays at the
                #          last char of the mention.
                start_iter = buf.get_iter_at_offset(insert_offset)
                end_iter   = buf.get_iter_at_offset(
                    insert_offset + len(inserted))
                start_mark = buf.create_mark(None, start_iter, False)
                end_mark   = buf.create_mark(None, end_iter,   True)
                self._pending_mentions.append({
                    "start":   start_mark,
                    "end":     end_mark,
                    "user_id": user_id,
                })
            # @everyone is server-detected: GroupMe scans the message
            # text for the literal string "@everyone" and adds the
            # broadcast attachment itself (with user_id=-1). Sending
            # our own attachment for it would result in duplicates, so
            # we only insert the text and let the server handle it.

            # Trailing space so the user can keep typing without manually
            # adding one.
            after_iter = buf.get_iter_at_offset(
                insert_offset + len(inserted))
            buf.insert(after_iter, " ")
        finally:
            self._in_mention_pick = False

        self._close_mention_popover()

    def _build_mentions_attachment(self, buf, text_offset_shift: int = 0):
        """Walk _pending_mentions and produce a GroupMe `mentions`
        attachment dict, or None if there are no mentions left.

        ``text_offset_shift`` is subtracted from each locus start to
        compensate for any leading whitespace stripped from the sent
        text (the buffer's offsets are based on the raw, un-stripped
        text, but the wire payload is stripped)."""
        if not self._pending_mentions:
            return None

        user_ids: list = []
        loci:     list = []

        for entry in self._pending_mentions:
            start_iter = buf.get_iter_at_mark(entry["start"])
            end_iter   = buf.get_iter_at_mark(entry["end"])
            start_off  = start_iter.get_offset() - text_offset_shift
            length     = end_iter.get_offset() - start_iter.get_offset()
            if length <= 0 or start_off < 0:
                continue
            user_ids.append(entry["user_id"])
            loci.append([start_off, length])

        if not user_ids:
            return None
        return {"type": "mentions", "user_ids": user_ids, "loci": loci}
