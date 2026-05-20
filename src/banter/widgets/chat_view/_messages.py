"""MessageListMixin — append / prepend bubbles, date separators,
scroll pinning, and the jump-to-bottom unread tracker.

Mixed into ChatView. Owns the heavy display logic between the raw
message list and the rendered Gtk.Box of MessageBubble + DateSeparator
widgets, plus the scroll-state machine used to keep the view pinned
to the bottom while attachments load.
"""

from datetime import datetime

from gi.repository import GLib, Gtk

from ...helpers import is_hidden_system_message
from ..message_bubble import MessageBubble
from ..misc import DateSeparator


class MessageListMixin:
    @staticmethod
    def _msg_date(msg):
        return datetime.fromtimestamp(msg.get("created_at", 0)).date()

    def _make_date_sep(self, d):
        return DateSeparator(d)

    def _make_bubble(self, msg):
        bubble = MessageBubble(
            msg, self._me, self._gid, self._api, self._win)
        mid = str(msg["id"])
        self._bubble_map[mid] = bubble
        if mid in self._pinned_ids:
            try:
                bubble.set_pinned(True)
            except Exception:
                pass
        return bubble

    def _set_initial(self, msgs):
        self._loading = False
        if not msgs:
            return
        # msgs is newest-first; iterate oldest-first to build top-down
        prev_date = None
        for m in reversed(msgs):
            if is_hidden_system_message(m):
                continue
            d = self._msg_date(m)
            if d != prev_date:
                self._msgs_box.append(self._make_date_sep(d))
                prev_date = d
            self._msgs_box.append(self._make_bubble(m))

        if msgs:
            self._oldest_id   = msgs[-1]["id"]
            self._newest_id   = msgs[0]["id"]
            self._oldest_date = self._msg_date(msgs[-1])
            self._newest_date = self._msg_date(msgs[0])
        GLib.idle_add(self._scroll_bottom)
        # Mark these messages as read on the server now that they're
        # rendered + scrolled into view (initial load lands at-bottom).
        self._send_read_receipt()
        # In DMs: also kick off a fetch for the OTHER user's read
        # pointer so we can stamp ✓ on our own bubbles they've seen.
        if self._is_dm:
            self._refresh_dm_read_receipt()

    def _prepend_old(self, msgs):
        self._loading = False
        if not msgs:
            self._load_more_btn.set_label("No more messages")
            self._load_more_btn.set_sensitive(False)
            return

        adj    = self._scroll.get_vadjustment()
        before = adj.get_upper()

        # msgs is newest-first from API; build widgets oldest-first so we
        # can detect date transitions, then insert in reverse so the oldest
        # ends up just after _load_more_btn.
        widgets   = []
        prev_date = None  # start with no "previous" — we add seps for each new date

        for m in reversed(msgs):   # oldest → newest
            if is_hidden_system_message(m):
                continue
            d = self._msg_date(m)
            if d != prev_date:
                widgets.append(self._make_date_sep(d))
                prev_date = d
            widgets.append(self._make_bubble(m))

        # If the newest batch message and the oldest existing message share a
        # date, the existing separator already covers that date — we can skip
        # the sep we'd otherwise show at the boundary.  Since we're inserting
        # *before* existing content, the boundary sep is the last widget in
        # our list only when it has the same date as _oldest_date.
        if (self._oldest_date is not None and
                isinstance(widgets[-1], DateSeparator) and
                self._msg_date(msgs[0]) == self._oldest_date):
            widgets.pop()

        # Insert reversed so oldest ends up first in the widget list
        for w in reversed(widgets):
            self._msgs_box.insert_child_after(w, self._load_more_btn)

        def restore():
            delta = adj.get_upper() - before
            adj.set_value(adj.get_value() + delta)

        GLib.idle_add(restore)
        if msgs:
            self._oldest_id   = msgs[-1]["id"]
            self._oldest_date = self._msg_date(msgs[-1])

    def _append_new(self, msgs):
        # msgs newest-first; iterate oldest-first to append in order.
        # The first new message becomes the "first unread" anchor that
        # the jump button will scroll to when the user is reading older
        # messages.
        appended_ids = []
        cleared_typers = False
        for m in reversed(msgs):
            if is_hidden_system_message(m):
                continue
            # A user finishing a message implicitly ends their typing
            # session. Drop them from the indicator so the bar doesn't
            # linger after their message lands.
            sender_uid = str(m.get("user_id", ""))
            if sender_uid and sender_uid in self._typing_users:
                self._typing_users.pop(sender_uid, None)
                cleared_typers = True
            d = self._msg_date(m)
            if d != self._newest_date:
                self._msgs_box.append(self._make_date_sep(d))
                self._newest_date = d
            self._msgs_box.append(self._make_bubble(m))
            appended_ids.append(str(m.get("id", "")))
        if cleared_typers:
            self._refresh_typing_indicator()
        if msgs:
            self._newest_id   = msgs[0]["id"]
            self._newest_date = self._msg_date(msgs[0])

        if self._at_bottom:
            # User is at the bottom — scroll to reveal new messages
            self._scroll_bottom()
            # …and tell the server we've seen them. _send_read_receipt
            # is a no-op if `_newest_id` hasn't actually advanced past
            # the last receipt we sent (e.g. our own outgoing echo).
            self._send_read_receipt()
        elif msgs:
            # Reading older messages: track unread state for the jump
            # button. msgs is newest-first, so msgs[-1] is the OLDEST of
            # the batch — that's the first one the user hasn't seen.
            if self._first_unread_id is None and appended_ids:
                self._first_unread_id = appended_ids[0]
            self._unread_count += len(msgs)
            self._unread_sender = msgs[0].get("name") or self._unread_sender
            self._update_jump_button()

    # ── Scroll-state machine ────────────────────────────────────────

    def _on_upper_changed(self, adj, _pspec):
        """Re-pin to the bottom when the scrollable area grows while
        the user is already at the bottom. Triggered by GTK measuring
        a newly-appended bubble or by a chat-view-wide image load."""
        if self._at_bottom:
            self._suppress_scroll_change = True
            try:
                adj.set_value(adj.get_upper())
            finally:
                self._suppress_scroll_change = False

    def _on_scroll_changed(self, adj):
        """Track whether the user is at the bottom of the message list."""
        # Our own pin set_value calls must not be misread as user scrolls.
        if self._suppress_scroll_change:
            return
        # Generous tolerance: image attachments and reaction rows can
        # bump `upper` by hundreds of px AFTER our set_value lands, and
        # the resulting value/upper mismatch must not be misread as the
        # user scrolling away (which would abandon the pin sequence).
        at_bottom = adj.get_value() >= (adj.get_upper() - adj.get_page_size() - 200)
        was_above = not self._at_bottom
        self._at_bottom = at_bottom
        if at_bottom:
            # User scrolled (or auto-scrolled) back to the bottom —
            # everything is now seen, hide the jump affordance.
            self._clear_unread_state()
            self._jump_btn.set_visible(False)
            # If they were scrolled up while new messages arrived (so
            # the read pointer didn't advance at the time), catch up
            # the receipt now. No-op when `_newest_id` matches the
            # last-sent value.
            if was_above:
                self._send_read_receipt()
        else:
            # While scrolled up, the jump button stays visible as a
            # quick way back regardless of whether new messages have
            # arrived.
            self._update_jump_button()

    def _clear_unread_state(self):
        self._first_unread_id = None
        self._unread_sender   = None
        self._unread_count    = 0

    def _update_jump_button(self):
        """Show/hide and label the jump button based on current scroll
        position and pending-unread state."""
        self._jump_btn.set_visible(not self._at_bottom)
        if self._unread_count == 0:
            self._jump_lbl.set_visible(False)
            self._jump_lbl.set_label("")
            return
        if self._unread_count == 1 and self._unread_sender:
            text = f"New message from {self._unread_sender}"
        elif self._unread_count == 1:
            text = "New message"
        else:
            text = f"{self._unread_count} new messages"
        self._jump_lbl.set_label(text)
        self._jump_lbl.set_visible(True)

    def _on_jump_clicked(self, *_):
        # Prefer scrolling to the first unread message so the user
        # actually sees what they missed; fall back to plain
        # scroll-to-bottom when there's no pending unread.
        target_id = self._first_unread_id
        if target_id and target_id in self._bubble_map:
            self._scroll_to_bubble(self._bubble_map[target_id])
        else:
            self._scroll_bottom()
        # Don't clear unread state here — _on_scroll_changed will do
        # that when the scroll actually reaches the bottom (i.e. the
        # user has caught up). This handles the case where the first-
        # unread is mid-screen, not at the bottom.

    def _scroll_to_bubble(self, bubble):
        """Scroll the message viewport so `bubble` is visible at the top
        of the visible area."""
        def _do_scroll():
            ok, rect = bubble.compute_bounds(self._msgs_box)
            if ok:
                adj = self._scroll.get_vadjustment()
                adj.set_value(max(0.0, rect.origin.y))
            return False
        GLib.idle_add(_do_scroll)

    def _scroll_bottom(self):
        """Reliably scroll the message list to the bottom of the last
        message.

        GTK4 measures wrapping labels with width-for-height, so the last
        bubble's height (especially with a multi-line text label and a
        reaction row) often isn't finalized for a frame or two after we
        append it. A single ``set_value(upper)`` lands a few pixels
        short, leaving the bubble visually clipped with no way to scroll
        further until the user manually scrolls up and back.

        Schedule pin attempts at increasing delays (0/50/150/350/700/
        1200 ms) so at least one fires after the slowest measure has
        settled. Each attempt cross-checks ``adj.upper`` against the
        last child's actual ``compute_bounds`` — sometimes the latter
        reflects new content before ``upper`` does."""
        self._at_bottom = True
        self._pin_seq  += 1
        seq = self._pin_seq
        for delay in (0, 50, 150, 350, 700, 1200, 2000):
            GLib.timeout_add(delay, self._pin_to_bottom_once, seq)

    def _pin_to_bottom_once(self, seq):
        # A newer _scroll_bottom invalidates older sequences. Note: we
        # do NOT bail on `not self._at_bottom` here — _at_bottom can be
        # spuriously cleared by an async value-changed when `upper`
        # grows from late layout passes, and we want the pin sequence
        # to ride that out. Always re-assert at_bottom while pinning.
        if seq != self._pin_seq:
            return False
        self._at_bottom = True

        # Force the message box and the last bubble to re-measure.
        # queue_resize is async but it bumps GTK's measure machinery
        # forward by a frame, which (combined with our staggered
        # timeouts) helps converge to the correct height faster.
        self._msgs_box.queue_resize()
        last = self._msgs_box.get_last_child()
        if last is not None:
            last.queue_resize()

        adj   = self._scroll.get_vadjustment()
        upper = adj.get_upper()
        # Cross-check upper against the last child's natural measured
        # height for the current allocated width — this often reflects
        # the bubble's true size before adj.upper has caught up.
        if last is not None:
            ok, rect = last.compute_bounds(self._msgs_box)
            if ok:
                content_bottom = rect.origin.y + rect.size.height
                width = self._msgs_box.get_width()
                if width > 0:
                    try:
                        _, nat_h, _, _ = last.measure(
                            Gtk.Orientation.VERTICAL, width)
                        content_bottom = max(content_bottom,
                                              rect.origin.y + nat_h)
                    except Exception:
                        pass
                upper = max(upper, content_bottom)

        # Diagnostic — re-enable when investigating scroll-pin issues:
        # log.debug("pin: seq=%d upper=%.1f page=%.1f value=%.1f",
        #     seq, upper, adj.get_page_size(), adj.get_value())

        self._suppress_scroll_change = True
        try:
            adj.set_value(upper)   # GTK clamps to upper - page_size
        finally:
            self._suppress_scroll_change = False
        return False
