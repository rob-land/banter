"""Banter — ChatView: message list + compose bar."""

import mimetypes
import time
from datetime import datetime
from pathlib import Path
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gdk, Gio

from ..constants import dbg
from ..async_utils import run_in_background
from ..api import GroupMeAPI
from ..helpers import is_hidden_system_message
from .mention_popover import MentionPopover, EVERYONE_ID
from .message_bubble import MessageBubble
from .misc import DateSeparator


class ChatView(Gtk.Box):
    DEFAULT_POLL_INTERVAL = 15_000   # ms

    def __init__(self, api: GroupMeAPI, group: dict, me_id, window,
                 is_dm: bool = False, other_user_id: str = None,
                 config=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._api          = api
        self._group        = group
        self._gid          = str(group["id"])
        self._me           = me_id
        self._win          = window
        self._is_dm        = is_dm
        self._config       = config
        # Conversation key used by the parent window to look up our
        # draft text. Uses the same key shape as MainWindow._rows.
        self._draft_key    = (
            "dm" if is_dm else "group",
            str(other_user_id) if (is_dm and other_user_id) else self._gid,
        )
        # For DMs: the other participant's user_id (used for fetch/send)
        self._other_uid    = str(other_user_id) if other_user_id else self._gid
        self._poll_ms      = self._read_poll_interval()
        self._oldest_id   = None
        self._newest_id   = None
        self._oldest_date = None
        self._newest_date = None
        self._loading     = False
        self._pending_img_url = None
        # Pending non-image file attachment. Filled in by _set_pending_file
        # after upload completes; _send turns this into a {type:file,file_id}
        # attachment and clears it.
        self._pending_file_id   = None
        self._pending_file_name = None
        self._poll_id    = None
        # Maps message id → MessageBubble widget for in-place refresh
        self._bubble_map : dict = {}
        # Pin-to-bottom machinery — see _scroll_bottom for why a single
        # set_value isn't enough. _pin_seq invalidates older pin
        # sequences when a new one starts; _suppress_scroll_change
        # blocks our own set_value calls from being misread as user
        # scrolls in _on_scroll_changed.
        self._pin_seq                = 0
        self._suppress_scroll_change = False
        # "Jump to bottom" / "new message" tracking. While the user is
        # scrolled up, _first_unread_id holds the id of the oldest
        # message that arrived since they scrolled away — clicking the
        # jump button takes them straight to it. _unread_sender holds
        # the most-recent sender's name for the button label.
        self._first_unread_id: str | None = None
        self._unread_sender:   str | None = None
        self._unread_count = 0
        # @-mention compose state. _mention_anchor is a TextMark at the
        # `@` character that opened the active autocomplete popover;
        # None means no popover is open. _pending_mentions holds the
        # marks-bracketed @name spans that have been picked but not yet
        # sent — at send time we read each pair of marks to recover
        # the (offset, length) for the mentions attachment, which
        # survives intermediate edits to the surrounding text.
        self._mention_anchor: "Gtk.TextMark | None" = None
        self._mention_popover: "MentionPopover | None" = None
        self._pending_mentions: list = []
        # Guard: set while we're driving the buffer ourselves (e.g.
        # replacing `@prefix` with `@<full name>`). Suppresses the
        # change-handler so it doesn't tear down the popover state
        # while we're still using it.
        self._in_mention_pick = False
        # Reply state. Set to a message dict when the user picks
        # "Reply" on a bubble; cleared on send or via the reply
        # preview's close button.
        self._reply_target: dict | None = None
        # Pinned-message ids for this conversation. Populated on demand
        # from /v3/pinned/* endpoints; bubbles render an indicator when
        # their id is in this set. The push stream doesn't carry pin
        # events, so the set is only authoritative at fetch time —
        # refetched whenever the user runs a pin/unpin action or opens
        # the pinned-messages dialog.
        self._pinned_ids: set = set()
        # Typing-indicator state.
        # _typing_users: uid → monotonic-deadline (s) at which the
        # indicator entry expires. Refreshed on each pulse received.
        # _typing_clear_id is a single GLib timeout that re-renders the
        # indicator bar when the soonest deadline elapses.
        self._typing_users: dict = {}
        self._typing_clear_id = 0
        # Outbound throttling: send at most one pulse every TYPING_PULSE_INTERVAL.
        self._last_typing_sent: float = 0.0
        self._build_widgets()
        self._fetch_pinned()

    TYPING_PULSE_INTERVAL = 3.0   # s — outbound rate-limit per conversation
    TYPING_DECAY_SECS     = 5.0   # s — how long a received pulse keeps a user "typing"

    def _read_poll_interval(self) -> int:
        if self._config:
            secs = self._config.get_pref("poll_interval_secs", 15)
            try:
                return max(5, int(secs)) * 1000
            except (TypeError, ValueError):
                pass
        return self.DEFAULT_POLL_INTERVAL

    def _build_widgets(self):
        """Build all child widgets. Called once from __init__."""
        # ── Message scroll ──
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_vexpand(True)
        self._scroll.set_policy(Gtk.PolicyType.NEVER,
                                 Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_kinetic_scrolling(True)
        # Critical: don't let child labels drive the window width wider.
        # With propagate_natural_width=False the ScrolledWindow reports a
        # fixed minimum width, and the viewport clips/wraps child content
        # to whatever width is actually allocated by the window.
        self._scroll.set_propagate_natural_width(False)

        # hexpand=True so the box fills the full allocated width, giving
        # each row_box (and therefore each bubble) a real pixel width to
        # wrap text into.
        self._msgs_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._msgs_box.set_hexpand(True)

        vp = Gtk.Viewport()
        vp.set_child(self._msgs_box)
        self._scroll.set_child(vp)

        # ── Jump-to-bottom button (and unread indicator) ──
        # Visible whenever the user has scrolled up. Shows just a down
        # arrow when there's nothing new; reveals a "New message[s]
        # from <name>" label when messages arrive while scrolled up.
        self._jump_lbl = Gtk.Label(label="")
        self._jump_lbl.set_visible(False)
        self._jump_lbl.add_css_class("dim-label")
        jump_icon = Gtk.Image.new_from_icon_name("go-down-symbolic")
        jump_content = Gtk.Box(spacing=8)
        jump_content.append(self._jump_lbl)
        jump_content.append(jump_icon)
        self._jump_btn = Gtk.Button()
        self._jump_btn.set_child(jump_content)
        self._jump_btn.add_css_class("new-msg-bar")
        self._jump_btn.add_css_class("suggested-action")
        self._jump_btn.set_halign(Gtk.Align.CENTER)
        self._jump_btn.set_valign(Gtk.Align.END)
        self._jump_btn.set_margin_bottom(8)
        self._jump_btn.set_visible(False)
        self._jump_btn.connect("clicked", self._on_jump_clicked)

        # Overlay the banner over the scroll window
        scroll_overlay = Gtk.Overlay()
        scroll_overlay.set_child(self._scroll)
        scroll_overlay.add_overlay(self._jump_btn)
        scroll_overlay.set_vexpand(True)

        # ── In-conversation search bar ──
        # Toggled by Ctrl+F or by the magnifying-glass header button.
        # Matches against the text of every loaded MessageBubble.
        # Up/Down arrows or the visible nav buttons cycle through
        # results; Escape closes.
        self._search_bar   = Gtk.SearchBar()
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search this chat…")
        self._search_entry.set_hexpand(True)

        prev_btn = Gtk.Button(icon_name="go-up-symbolic")
        prev_btn.add_css_class("flat")
        prev_btn.set_tooltip_text("Previous match")
        prev_btn.connect("clicked",
                          lambda *_: self._chat_search_step(-1))
        next_btn = Gtk.Button(icon_name="go-down-symbolic")
        next_btn.add_css_class("flat")
        next_btn.set_tooltip_text("Next match")
        next_btn.connect("clicked",
                          lambda *_: self._chat_search_step(1))
        # Status label like "3 of 17" so the user knows where they are
        # in the result set.
        self._search_status = Gtk.Label(label="")
        self._search_status.add_css_class("dim-label")
        self._search_status.set_margin_start(8)
        self._search_status.set_margin_end(8)

        search_box = Gtk.Box(spacing=4)
        search_box.append(self._search_entry)
        search_box.append(self._search_status)
        search_box.append(prev_btn)
        search_box.append(next_btn)
        self._search_bar.set_child(search_box)
        self._search_bar.connect_entry(self._search_entry)
        self._search_bar.set_show_close_button(True)
        self._search_entry.connect("search-changed",
                                     self._on_chat_search_changed)
        self._search_entry.connect("next-match",
                                     lambda *_: self._chat_search_step(1))
        self._search_entry.connect("previous-match",
                                     lambda *_: self._chat_search_step(-1))
        self._search_entry.connect("activate",
                                     lambda *_: self._chat_search_step(1))
        self._search_matches: list = []   # ordered list of MessageBubble
        self._search_index: int   = 0

        # Track scroll position to decide auto-scroll vs. banner
        self._at_bottom = True
        adj = self._scroll.get_vadjustment()
        adj.connect("value-changed", self._on_scroll_changed)
        # Pin to the bottom when the visible-content height grows while
        # the user is already at the bottom. Without this, async image
        # loads (and the slight delay between appending a new bubble
        # and GTK measuring it) leave the scroll position at the OLD
        # bottom, which is now mid-screen — so the user sees the new
        # message briefly and then it scrolls "off" as layout settles.
        adj.connect("notify::upper", self._on_upper_changed)

        # ── Pending image preview ──
        self._preview_bar = Gtk.Box(spacing=8)
        self._preview_bar.set_margin_start(12)
        self._preview_bar.set_margin_end(12)
        self._preview_bar.set_visible(False)
        self._preview_label = Gtk.Label()
        self._preview_label.set_ellipsize(3)
        self._preview_bar.append(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
        self._preview_bar.append(self._preview_label)

        clear_btn = Gtk.Button(icon_name="window-close-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", self._clear_attachment)
        self._preview_bar.append(clear_btn)

        # ── Typing indicator ──
        # Single-line dim label shown above the compose entry whenever
        # at least one other user is mid-pulse. Hidden by default;
        # _refresh_typing_indicator toggles visibility and label text.
        self._typing_bar = Gtk.Box(spacing=6)
        self._typing_bar.set_margin_start(12)
        self._typing_bar.set_margin_end(12)
        self._typing_bar.set_visible(False)
        self._typing_lbl = Gtk.Label(label="")
        self._typing_lbl.add_css_class("dim-label")
        self._typing_lbl.set_xalign(0)
        self._typing_lbl.set_hexpand(True)
        self._typing_bar.append(self._typing_lbl)

        # ── Reply preview ──
        # Shown when the user picks "Reply" on a bubble. Sits in the
        # bottom-bar stack just like the image preview so it stays
        # above the compose entry but below the scroll viewport.
        self._reply_bar = Gtk.Box(spacing=8)
        self._reply_bar.set_margin_start(12)
        self._reply_bar.set_margin_end(12)
        self._reply_bar.set_visible(False)
        self._reply_bar.append(
            Gtk.Image.new_from_icon_name("mail-reply-sender-symbolic"))
        self._reply_label = Gtk.Label(xalign=0)
        self._reply_label.set_ellipsize(3)
        self._reply_label.set_hexpand(True)
        self._reply_bar.append(self._reply_label)
        reply_close = Gtk.Button(icon_name="window-close-symbolic")
        reply_close.add_css_class("flat")
        reply_close.connect("clicked",
                             lambda *_: self.set_reply_target(None))
        self._reply_bar.append(reply_close)

        # ── Compose bar ──
        compose = Gtk.Box(spacing=8)
        compose.add_css_class("compose-bar")

        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic")
        attach_btn.add_css_class("flat")
        attach_btn.add_css_class("compose-btn")
        attach_btn.set_tooltip_text("Attach file or image")
        attach_btn.set_valign(Gtk.Align.END)
        attach_btn.connect("clicked", self._pick_attachment)
        compose.append(attach_btn)

        self._entry = Gtk.TextView()
        self._entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._entry.set_accepts_tab(False)
        self._entry.set_pixels_above_lines(4)
        self._entry.set_pixels_below_lines(4)
        # Restore any saved draft for this conversation. _on_buf_changed
        # isn't wired up yet, so this set_text doesn't trigger the
        # @-mention detector spuriously.
        drafts = getattr(self._win, "_drafts", None) or {}
        self._entry.get_buffer().set_text(
            drafts.get(self._draft_key, ""))

        entry_scroll = Gtk.ScrolledWindow()
        entry_scroll.set_child(self._entry)
        entry_scroll.set_policy(Gtk.PolicyType.NEVER,
                                  Gtk.PolicyType.AUTOMATIC)
        entry_scroll.set_min_content_height(44)
        entry_scroll.set_max_content_height(120)
        entry_scroll.set_propagate_natural_height(True)
        entry_scroll.set_hexpand(True)
        entry_scroll.set_valign(Gtk.Align.CENTER)
        compose.append(entry_scroll)

        send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        send_btn.add_css_class("suggested-action")
        send_btn.add_css_class("compose-btn")
        send_btn.set_tooltip_text("Send (Enter)")
        send_btn.set_valign(Gtk.Align.END)
        send_btn.connect("clicked", self._send)
        compose.append(send_btn)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self._entry.add_controller(key_ctrl)

        # ── @-mention autocomplete (groups only) ──
        # The buffer-changed handler is wired up unconditionally so that
        # `@` keystrokes are still recognized after the (async) member
        # fetch completes — see _ensure_mention_popover.
        if not self._is_dm:
            self._entry.get_buffer().connect(
                "changed", self._on_buf_changed)
            self._entry.get_buffer().connect(
                "changed", self._on_buf_changed_typing)
            self._fetch_members_for_mentions()

        # ── Assemble via Adw.ToolbarView so the compose + preview bars
        #     sit in bottom-bar slots that the compositor treats as
        #     keyboard-avoiding safe areas. On Phosh/squeekboard this
        #     causes the window content to shrink when the OSK opens so
        #     the compose bar stays visible above the keyboard. ──
        tv = Adw.ToolbarView()
        tv.set_vexpand(True)
        # SearchBar lives at the top of the chat content; revealed by
        # the toggle_search() window action (Ctrl+F).
        tv.add_top_bar(self._search_bar)
        tv.set_content(scroll_overlay)
        # add_bottom_bar stacks in order added, so reply preview and
        # image preview sit above the compose entry.
        tv.add_bottom_bar(self._typing_bar)
        tv.add_bottom_bar(self._reply_bar)
        tv.add_bottom_bar(self._preview_bar)
        tv.add_bottom_bar(compose)
        self.append(tv)

        # ── "Load more" sentinel at top ──
        self._load_more_btn = Gtk.Button(label="↑  Load older messages")
        self._load_more_btn.add_css_class("flat")
        self._load_more_btn.set_margin_top(8)
        self._load_more_btn.set_margin_bottom(8)
        self._load_more_btn.connect("clicked", self._load_more)
        self._msgs_box.append(self._load_more_btn)

        self._fetch_messages()
        self._start_polling()

    def restart_poll(self):
        """Cancel + restart the DM fallback poll with the freshly-saved
        interval. No-op for group chats (they use the shared push
        client)."""
        if not self._is_dm:
            return
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        self._poll_ms = self._read_poll_interval()
        self._start_polling()

    # ── Lifecycle ──
    def stop(self):
        """Stop fallback DM poll timer (push lives in MainWindow)."""
        # Persist any in-progress compose text into the parent window's
        # draft store so it's restored next time this conversation is
        # opened. We always write — including the empty string — so a
        # stale draft from a previous switch is properly cleared.
        try:
            buf  = self._entry.get_buffer()
            text = buf.get_text(buf.get_start_iter(),
                                 buf.get_end_iter(), False)
            drafts = getattr(self._win, "_drafts", None)
            if drafts is not None:
                if text:
                    drafts[self._draft_key] = text
                else:
                    drafts.pop(self._draft_key, None)
        except Exception:
            pass

        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        if self._typing_clear_id:
            try: GLib.source_remove(self._typing_clear_id)
            except Exception: pass
            self._typing_clear_id = 0
        # Bumping the seq invalidates any in-flight pin timeouts so they
        # short-circuit instead of touching a destroyed adjustment.
        self._pin_seq += 1
        if self._mention_popover is not None:
            self._mention_popover.unparent()
            self._mention_popover = None
            self._mention_anchor = None

    def _start_polling(self):
        """Groups use the MainWindow-level push client.
        DMs fall back to periodic polling since push events don't
        reliably include DM group IDs."""
        if self._is_dm:
            dbg("ChatView: using polling fallback (DM)")
            self._poll_id = GLib.timeout_add(self._poll_ms, self._poll)
        else:
            dbg("ChatView: push handled by MainWindow singleton")

    def _on_push_event(self, data: dict):
        """Handle a push event received from GroupMe's Faye server."""
        ev_type = data.get("type", "")
        subject = data.get("subject", {})
        dbg("push event: type=%s subject_keys=%s", ev_type, list(subject.keys()))

        if ev_type == "typing":
            # /group/{gid} flat event: {"type":"typing","user_id":"...","started":<ms>}.
            # Group-only for now — DM typing channel hasn't been captured.
            if self._is_dm:
                return
            uid = str(data.get("user_id", ""))
            if not uid or uid == str(self._me):
                return
            self._on_typing_received(uid)
            return

        if ev_type == "line.create":
            if str(subject.get("group_id", "")) == self._gid:
                # Sender finished typing — drop them from the indicator.
                sender_uid = str(subject.get("user_id", ""))
                if sender_uid and sender_uid in self._typing_users:
                    self._typing_users.pop(sender_uid, None)
                    self._refresh_typing_indicator()
                msg_id = str(subject.get("id", ""))
                if msg_id and msg_id in self._bubble_map:
                    # Already displayed (we sent it ourselves or received a duplicate)
                    # — refresh reactions in case server data differs
                    dbg("push: line.create duplicate ignored for msg %s", msg_id)
                    self._bubble_map[msg_id].refresh(subject)
                else:
                    self._append_new([subject])

        elif ev_type in ("line.update", "message.update", "line.edit"):
            # Edit notification — replace the bubble's text and stamp
            # an "(edited)" indicator. The server may surface the new
            # text directly in `subject` or nested under `line`.
            line   = subject.get("line") or subject
            msg_id = str(line.get("id") or subject.get("line_id") or
                          subject.get("message_id") or "")
            if msg_id and msg_id in self._bubble_map:
                self._bubble_map[msg_id].update_text_from(line)

        elif ev_type == "line.destroy" or ev_type == "line.delete":
            line   = subject.get("line") or subject
            msg_id = str(line.get("id") or subject.get("line_id") or
                          subject.get("message_id") or "")
            if msg_id and msg_id in self._bubble_map:
                bubble = self._bubble_map.pop(msg_id)
                parent = bubble.get_parent()
                if parent is not None:
                    parent.remove(bubble)

        elif ev_type in ("like.create", "like.delete",
                          "favorite.create", "favorite.destroy",
                          "reaction.create", "reaction.destroy"):
            # GroupMe sends like events with the message nested under "line":
            # {"type":"like.create","subject":{"line":{msg},"reactions":[...],...}}
            line     = subject.get("line") or {}
            msg_id   = str(line.get("id") or
                           subject.get("line_id") or
                           subject.get("message_id") or
                           subject.get("id") or "")
            group_id = str(line.get("group_id") or
                           subject.get("group_id") or
                           subject.get("conversation_id") or "")
            # Log the raw reaction payload so we can see which pack / emoji
            # the originating client picked — useful for tracking down
            # packs the /powerups catalog doesn't surface.
            dbg("push: reaction event msg_id=%s group_id=%s gid=%s in_map=%s user_reaction=%s",
                msg_id, group_id, self._gid, msg_id in self._bubble_map,
                subject.get("user_reaction"))

            if msg_id and msg_id in self._bubble_map:
                self._bubble_map[msg_id].refresh_from_server()
            elif msg_id and line and (group_id == self._gid or not group_id):
                # We have the full message in the push payload — use it directly
                # rather than making an extra API call
                bubble = self._bubble_map.get(msg_id)
                if bubble:
                    GLib.idle_add(bubble.refresh, line)

        elif ev_type == "ping":
            pass

    def _poll(self):
        """Fallback periodic poll used only when push is unavailable (DMs)."""
        if not self._newest_id:
            return True

        oldest_id = self._oldest_id
        newest_id = self._newest_id
        is_dm     = self._is_dm
        other_uid = self._other_uid
        gid       = self._gid

        def worker():
            if is_dm:
                new_msgs = self._api.get_dm_messages(
                    other_uid, since_id=newest_id, limit=20)
                refreshed = []
            else:
                new_msgs  = self._api.get_messages(
                    gid, since_id=newest_id, limit=20)
                refreshed = self._api.get_messages(
                    gid, after_id=oldest_id, limit=20) if oldest_id else []
            GLib.idle_add(self._on_poll_result, new_msgs, refreshed)

        run_in_background(worker)
        return True

    def _on_poll_result(self, new_msgs, refreshed):
        if new_msgs:
            self._append_new(new_msgs)
        for msg in refreshed:
            mid = str(msg.get("id", ""))
            if mid in self._bubble_map:
                self._bubble_map[mid].refresh(msg)

    # ── Fetching ──
    def _fetch_messages(self):
        if self._loading:
            return
        self._loading = True

        def worker():
            if self._is_dm:
                msgs = self._api.get_dm_messages(self._other_uid, limit=30)
            else:
                msgs = self._api.get_messages(self._gid, limit=30)
            GLib.idle_add(self._set_initial, msgs)

        run_in_background(worker)

    # ── Typing indicator ──
    def _on_buf_changed_typing(self, _buf):
        """Throttled outbound typing pulse. Pulses are best-effort — the
        push client silently drops them while reconnecting."""
        # Don't pulse if compose is empty (deleting the last char shouldn't
        # tell anyone we're "typing"). Don't pulse from DMs — channel
        # format is unverified.
        if self._is_dm:
            return
        buf  = self._entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(),
                              buf.get_end_iter(), False)
        if not text.strip():
            return
        now = time.monotonic()
        if (now - self._last_typing_sent) < self.TYPING_PULSE_INTERVAL:
            return
        push = getattr(self._win, "_push", None)
        if push is None:
            return
        if push.publish_typing_group(self._gid):
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

    # ── Pinned messages ──
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

    # ── Display helpers ──
    @staticmethod
    def _msg_date(msg):
        return datetime.fromtimestamp(msg.get("created_at", 0)).date()

    def _make_date_sep(self, d):
        return DateSeparator(d)

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
        for m in reversed(msgs):
            if is_hidden_system_message(m):
                continue
            d = self._msg_date(m)
            if d != self._newest_date:
                self._msgs_box.append(self._make_date_sep(d))
                self._newest_date = d
            self._msgs_box.append(self._make_bubble(m))
            appended_ids.append(str(m.get("id", "")))
        if msgs:
            self._newest_id   = msgs[0]["id"]
            self._newest_date = self._msg_date(msgs[0])

        if self._at_bottom:
            # User is at the bottom — scroll to reveal new messages
            self._scroll_bottom()
        elif msgs:
            # Reading older messages: track unread state for the jump
            # button. msgs is newest-first, so msgs[-1] is the OLDEST of
            # the batch — that's the first one the user hasn't seen.
            if self._first_unread_id is None and appended_ids:
                self._first_unread_id = appended_ids[0]
            self._unread_count += len(msgs)
            self._unread_sender = msgs[0].get("name") or self._unread_sender
            self._update_jump_button()

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

    def _add_bubble(self, msg, append=True):
        """Legacy single-message helper (used by _on_sent)."""
        d = self._msg_date(msg)
        if append:
            if d != self._newest_date:
                self._msgs_box.append(self._make_date_sep(d))
                self._newest_date = d
            self._msgs_box.append(self._make_bubble(msg))
        else:
            self._msgs_box.insert_child_after(
                self._make_bubble(msg), self._load_more_btn)

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
        self._at_bottom = at_bottom
        if at_bottom:
            # User scrolled (or auto-scrolled) back to the bottom —
            # everything is now seen, hide the jump affordance.
            self._clear_unread_state()
            self._jump_btn.set_visible(False)
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
        # dbg("pin: seq=%d upper=%.1f page=%.1f value=%.1f",
        #     seq, upper, adj.get_page_size(), adj.get_value())

        self._suppress_scroll_change = True
        try:
            adj.set_value(upper)   # GTK clamps to upper - page_size
        finally:
            self._suppress_scroll_change = False
        return False

    # ── @-mentions ──
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

    # ── In-conversation search ──
    def toggle_search(self):
        """Show/hide the chat search bar. Called by MainWindow's
        Ctrl+F action."""
        new_state = not self._search_bar.get_search_mode()
        self._search_bar.set_search_mode(new_state)
        if new_state:
            self._search_entry.grab_focus()
        else:
            self._clear_search_highlights()

    def _on_chat_search_changed(self, entry):
        query = entry.get_text().strip().lower()
        self._clear_search_highlights()
        self._search_matches = []
        if not query:
            self._search_status.set_text("")
            return
        # Walk the message box in order; collect bubbles whose text
        # contains the query, then highlight them.
        child = self._msgs_box.get_first_child()
        while child is not None:
            if isinstance(child, MessageBubble):
                text = (child.msg.get("text") or "").lower()
                if query in text:
                    child.add_css_class("search-match")
                    self._search_matches.append(child)
            child = child.get_next_sibling()
        if not self._search_matches:
            self._search_status.set_text("No matches")
            return
        # Jump to the most recent (last) match by default — matches the
        # convention where the user is usually looking for something
        # they recently saw.
        self._search_index = len(self._search_matches) - 1
        self._update_search_status()
        self._scroll_to_bubble(self._search_matches[self._search_index])

    def _chat_search_step(self, direction: int):
        if not self._search_matches:
            return
        n = len(self._search_matches)
        self._search_index = (self._search_index + direction) % n
        self._update_search_status()
        self._scroll_to_bubble(self._search_matches[self._search_index])

    def _update_search_status(self):
        n = len(self._search_matches)
        if n == 0:
            self._search_status.set_text("")
        else:
            self._search_status.set_text(
                f"{self._search_index + 1} of {n}")

    def _clear_search_highlights(self):
        for b in self._search_matches:
            try:
                b.remove_css_class("search-match")
            except Exception:
                pass
        self._search_matches = []
        self._search_index   = 0
        self._search_status.set_text("")

    # ── Reply ──
    def set_reply_target(self, msg: dict | None):
        """Select a message to reply to (or pass None to clear). The
        compose-bar preview updates and the next send will attach a
        `reply` attachment pointing at it. Called by the message
        bubble's context menu."""
        self._reply_target = msg
        if msg is None:
            self._reply_bar.set_visible(False)
            self._reply_label.set_text("")
            return
        sender = msg.get("name") or "Unknown"
        body   = (msg.get("text") or "").strip()
        if not body:
            atts = msg.get("attachments") or []
            if any(a.get("type") == "image" for a in atts):
                body = "📷 Image"
            elif atts:
                body = "📎 Attachment"
            else:
                body = "…"
        # Single-line preview; ellipsize handles overflow visually
        self._reply_label.set_text(f"Replying to {sender}: {body}")
        self._reply_bar.set_visible(True)
        # Keyboard focus back to the entry so the user can just type
        self._entry.grab_focus()

    def _clear_pending_mentions(self):
        buf = self._entry.get_buffer()
        for entry in self._pending_mentions:
            try:
                buf.delete_mark(entry["start"])
                buf.delete_mark(entry["end"])
            except Exception:
                pass
        self._pending_mentions = []

    # ── Sending ──
    def _on_key(self, ctrl, keyval, keycode, state):
        # Mention popover steals navigation/accept keys while open.
        if self._mention_anchor is not None and self._mention_popover:
            if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
                self._mention_popover.navigate(-1)
                return True
            if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
                self._mention_popover.navigate(1)
                return True
            if keyval == Gdk.KEY_Escape:
                self._close_mention_popover()
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
                if self._mention_popover.accept():
                    return True
                self._close_mention_popover()
                return False

        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Return and not shift:
            self._send()
            return True
        return False

    def _send(self, *_):
        buf      = self._entry.get_buffer()
        raw_text = buf.get_text(
            buf.get_start_iter(), buf.get_end_iter(), False)
        text     = raw_text.strip()

        if not text and not self._pending_img_url and not self._pending_file_id:
            return

        # Build the mentions attachment from pending marks BEFORE we
        # clear the buffer (clearing destroys the marks). loci offsets
        # are anchored in the raw buffer text; subtract leading
        # whitespace to align them with the stripped wire text.
        leading = len(raw_text) - len(raw_text.lstrip())
        mentions_att = self._build_mentions_attachment(buf, leading)
        self._clear_pending_mentions()

        buf.set_text("")
        atts = []
        if self._pending_img_url:
            atts.append({"type": "image", "url": self._pending_img_url})
            self._clear_attachment()
        elif self._pending_file_id:
            atts.append({"type": "file", "file_id": self._pending_file_id})
            self._clear_attachment()
        if mentions_att:
            atts.append(mentions_att)
        if self._reply_target is not None:
            reply_id = str(self._reply_target.get("id", ""))
            if reply_id:
                atts.append({
                    "type":          "reply",
                    "reply_id":      reply_id,
                    "base_reply_id": reply_id,
                })
            self.set_reply_target(None)

        def worker():
            if self._is_dm:
                msg = self._api.send_dm(
                    self._other_uid, text, atts or None)
            else:
                msg = self._api.send_message(
                    self._gid, text, atts or None)
            if msg:
                GLib.idle_add(self._on_sent, msg)
            else:
                GLib.idle_add(
                    lambda: self._win.toast("Failed to send message"))

        run_in_background(worker)

    def _on_sent(self, msg):
        self._add_bubble(msg, append=True)
        if msg:
            self._newest_id = msg["id"]
        self._scroll_bottom()

    # ── Attachments (image / file) ──
    def _conv_id(self) -> str:
        """API conversation_id for /conversations/ and /file.groupme.com/v1/
        endpoints — `_gid` for groups, `<lo>+<hi>` for DMs."""
        if not self._is_dm:
            return self._gid
        try:
            a, b = int(self._me), int(self._other_uid)
            lo, hi = (a, b) if a < b else (b, a)
            return f"{lo}+{hi}"
        except (TypeError, ValueError):
            return f"{self._me}+{self._other_uid}"

    def _pick_attachment(self, *_):
        """Open the system file picker. Picked files are routed by MIME
        type: images via the existing image-upload path (rendered inline
        on receivers), everything else via the file-upload path
        (rendered as a download link)."""
        fd = Gtk.FileDialog()
        fd.set_title("Attach file")
        fd.open(self._win, None, self._on_attachment_picked)

    def _on_attachment_picked(self, fd, result):
        try:
            f    = fd.open_finish(result)
            path = f.get_path()
        except GLib.Error:
            return

        mime, _ = mimetypes.guess_type(path)
        is_image = bool(mime and mime.startswith("image/"))

        if is_image:
            self._win.toast("Uploading image…")
            def worker():
                url = self._api.upload_image(path)
                if url:
                    GLib.idle_add(self._set_pending_image, url,
                                   Path(path).name)
                else:
                    GLib.idle_add(lambda: self._win.toast(
                        "Image upload failed"))
            run_in_background(worker)
            return

        # Non-image: file attachment. Works for both groups and DMs;
        # the upload URL is /v1/{conv_id}/files where conv_id is the
        # group_id for groups or "<lo>+<hi>" for DMs.
        self._win.toast("Uploading file…")
        cid  = self._conv_id()
        name = Path(path).name
        def worker():
            file_id = self._api.upload_file(cid, path)
            if file_id:
                GLib.idle_add(self._set_pending_file, file_id, name)
            else:
                GLib.idle_add(lambda: self._win.toast(
                    "File upload failed"))
        run_in_background(worker)

    def _set_pending_image(self, url, name):
        self._pending_img_url   = url
        self._pending_file_id   = None
        self._pending_file_name = None
        self._preview_label.set_text(name)
        self._preview_bar.set_visible(True)
        self._win.toast("Image ready – press send")

    def _set_pending_file(self, file_id, name):
        self._pending_file_id   = file_id
        self._pending_file_name = name
        self._pending_img_url   = None
        self._preview_label.set_text(f"📎  {name}")
        self._preview_bar.set_visible(True)
        self._win.toast("File ready – press send")

    def _clear_attachment(self, *_):
        self._pending_img_url   = None
        self._pending_file_id   = None
        self._pending_file_name = None
        self._preview_bar.set_visible(False)


