"""StatusMixin — pending / failed / pin / DM-read indicators and the
state transitions between them.

Mixed into MessageBubble. Reads/writes instance state set up in the
host class's `__init__`: `self._ts_box`, `self._pending_spinner`,
`self._pending_status_lbl`, `self._action_row`, `self._bubble_inner`,
`self._pin_icon`, `self._read_icon`, `self.is_pending`, `self.is_failed`,
`self.msg`, `self.win`.

Also calls into `ReactionsMixin._build_reactions_row` and
`_render_reactions` plus `ContextMenuMixin._wire_context_menu` from
`transition_to_sent` — both reachable via MRO on the host class.
"""

from datetime import datetime

from gi.repository import Gtk


class StatusMixin:
    # ── pending / failed state ────────────────────────────────────
    def _add_pending_indicator(self):
        """Show a small spinner + 'Sending…' caption next to the
        timestamp. Fixed by `transition_to_sent` or replaced with the
        failed indicator by `transition_to_failed`."""
        if self._ts_box is None:
            return
        self._pending_spinner = Gtk.Spinner(spinning=True)
        self._pending_spinner.set_valign(Gtk.Align.CENTER)
        self._pending_status_lbl = Gtk.Label(label="Sending…")
        self._pending_status_lbl.add_css_class("dim-caption")
        # Insert at the start of ts_box so they appear before the time.
        self._ts_box.prepend(self._pending_status_lbl)
        self._ts_box.prepend(self._pending_spinner)

    def _remove_pending_indicator(self):
        if self._pending_spinner is not None:
            self._pending_spinner.set_spinning(False)
            parent = self._pending_spinner.get_parent()
            if parent is not None:
                parent.remove(self._pending_spinner)
            self._pending_spinner = None
        if self._pending_status_lbl is not None:
            parent = self._pending_status_lbl.get_parent()
            if parent is not None:
                parent.remove(self._pending_status_lbl)
            self._pending_status_lbl = None

    def _add_failed_indicator(self):
        """Replace the 'Sending…' caption with a failed-state caption
        and append an inline Retry / Discard action row to the bubble.
        Action handlers route through ChatView (see _action_retry /
        _action_discard)."""
        if self._ts_box is None:
            return
        # Failed caption
        self._pending_status_lbl = Gtk.Label(label="Failed to send")
        self._pending_status_lbl.add_css_class("error")
        self._pending_status_lbl.add_css_class("caption")
        icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
        icon.add_css_class("error")
        self._ts_box.prepend(self._pending_status_lbl)
        self._ts_box.prepend(icon)

        # Inline action row (Retry / Discard)
        self._action_row = Gtk.Box(spacing=8)
        self._action_row.set_halign(Gtk.Align.END)
        self._action_row.set_margin_top(4)
        retry_btn = Gtk.Button(label="Retry")
        retry_btn.add_css_class("flat")
        retry_btn.connect("clicked", lambda *_: self._action_retry())
        discard_btn = Gtk.Button(label="Discard")
        discard_btn.add_css_class("flat")
        discard_btn.add_css_class("destructive-action")
        discard_btn.connect("clicked", lambda *_: self._action_discard())
        self._action_row.append(retry_btn)
        self._action_row.append(discard_btn)
        self._bubble_inner.append(self._action_row)

    def _remove_failed_indicator(self):
        if self._action_row is not None:
            parent = self._action_row.get_parent()
            if parent is not None:
                parent.remove(self._action_row)
            self._action_row = None
        # The failed caption shares the same _pending_status_lbl slot
        # as the sending caption — _remove_pending_indicator handles it
        # whether it's "Sending…" or "Failed to send".
        self._remove_pending_indicator()
        # Strip the icon we prepended in _add_failed_indicator.
        if self._ts_box is not None:
            first = self._ts_box.get_first_child()
            if isinstance(first, Gtk.Image):
                self._ts_box.remove(first)

    def transition_to_sent(self, server_msg: dict):
        """Promote a pending bubble to a real, sent one. Replaces the
        local synthetic msg with the server response, removes the
        pending visual marker, and lazily builds the reactions row +
        context menu (deferred from __init__ for pending bubbles)."""
        self.is_pending = False
        self.is_failed  = False
        self.msg = server_msg
        self._bubble_inner.remove_css_class("pending")
        self._remove_pending_indicator()
        self._remove_failed_indicator()
        self._build_reactions_row()
        self._render_reactions(server_msg.get("reactions", []),
                               server_msg.get("favorited_by", []))
        self._wire_context_menu()

    def transition_to_failed(self):
        """Move a pending bubble to the failed state — error caption +
        Retry/Discard buttons."""
        self.is_pending = False
        self.is_failed  = True
        self._remove_pending_indicator()
        self._bubble_inner.remove_css_class("pending")
        self._bubble_inner.add_css_class("failed")
        self._add_failed_indicator()

    def transition_to_pending(self):
        """Re-enter the pending state (for retry from failed)."""
        self.is_pending = True
        self.is_failed  = False
        self._remove_failed_indicator()
        self._bubble_inner.remove_css_class("failed")
        self._bubble_inner.add_css_class("pending")
        self._add_pending_indicator()

    def _action_retry(self):
        cv = getattr(self.win, "_chat_view", None)
        if cv is not None and hasattr(cv, "retry_pending_send"):
            cv.retry_pending_send(self)

    def _action_discard(self):
        cv = getattr(self.win, "_chat_view", None)
        if cv is not None and hasattr(cv, "discard_pending"):
            cv.discard_pending(self)

    # ── pin indicator ────────────────────────────────────────────
    def _make_pin_icon(self):
        img = Gtk.Image.new_from_icon_name("view-pin-symbolic")
        img.set_pixel_size(12)
        img.add_css_class("dim-label")
        img.set_tooltip_text("Pinned")
        img.set_visible(False)
        return img

    def set_pinned(self, pinned: bool):
        """Show / hide the pin indicator on this bubble. Called by
        ChatView when the conversation's pinned set changes."""
        if self._pin_icon is not None:
            self._pin_icon.set_visible(bool(pinned))

    def _is_pinned(self) -> bool:
        cv = getattr(self.win, "_chat_view", None)
        if cv is None:
            return False
        try:
            return cv.is_pinned(self.msg.get("id"))
        except Exception:
            return False

    # ── DM "Read" indicator ─────────────────────────────────────
    def _make_read_icon(self):
        # Single check icon — kept dim so it doesn't compete with the
        # bubble content. Tooltip is filled in by set_read() with the
        # exact "Read at HH:MM" so the user can tell when.
        img = Gtk.Image.new_from_icon_name("object-select-symbolic")
        img.set_pixel_size(12)
        img.add_css_class("dim-label")
        img.set_visible(False)
        return img

    def set_read(self, read_at):
        """Show / hide the Read indicator (own DM bubbles only).

        `read_at` is the unix timestamp from the other user's
        read_receipt — used for the tooltip — or None to hide."""
        if self._read_icon is None:
            return
        if not read_at:
            self._read_icon.set_visible(False)
            return
        try:
            tip = "Read " + datetime.fromtimestamp(
                int(read_at)).strftime("%-I:%M %p")
        except Exception:
            tip = "Read"
        self._read_icon.set_tooltip_text(tip)
        self._read_icon.set_visible(True)
