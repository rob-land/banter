"""ChatView — message list + compose bar. Public class.

The constructor wires up instance state shared across the feature
mixins (`_messages`, `_polling`, `_typing`, `_pinned`, `_jump_to_date`,
`_mentions`, `_search`, `_sending`, `_read_receipts`, `_attachments`,
`_voice`). `_build_widgets` constructs the Gtk widget tree those
mixins manipulate. Method-bag behavior lives in the mixins; the host
class itself owns only layout + lifecycle teardown.
"""

from gi.repository import Adw, GLib, Gtk

from ...api import GroupMeAPI
from ..mention_popover import MentionPopover
from ._attachments import AttachmentsMixin
from ._jump_to_date import JumpToDateMixin
from ._mentions import MentionsMixin
from ._messages import MessageListMixin
from ._pinned import PinnedMixin
from ._polling import PollingMixin
from ._read_receipts import ReadReceiptsMixin
from ._search import SearchMixin
from ._sending import SendingMixin
from ._typing import TypingMixin
from ._voice import VoiceMixin


class ChatView(
    MessageListMixin,
    PollingMixin,
    TypingMixin,
    PinnedMixin,
    JumpToDateMixin,
    MentionsMixin,
    SearchMixin,
    SendingMixin,
    ReadReceiptsMixin,
    AttachmentsMixin,
    VoiceMixin,
    Gtk.Box,
):
    DEFAULT_POLL_INTERVAL = 15_000   # ms
    TYPING_PULSE_INTERVAL = 3.0   # s — outbound rate-limit per conversation
    TYPING_DECAY_SECS     = 5.0   # s — how long a received pulse keeps a user "typing"

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
        # draft text. Uses the same key shape as BanterWindow._rows.
        self._draft_key    = (
            "dm" if is_dm else "group",
            str(other_user_id) if (is_dm and other_user_id) else self._gid,
        )
        # For DMs: the other participant's user_id (used for fetch/send)
        self._other_uid    = str(other_user_id) if other_user_id else self._gid
        self._poll_ms      = self.DEFAULT_POLL_INTERVAL
        self._oldest_id   = None
        self._newest_id   = None
        # Last message-id we successfully POSTed a read receipt for.
        # Throttle guard: only fire a new receipt when `_newest_id`
        # advances past this. None means we've never sent one for
        # this ChatView instance.
        self._last_receipt_id : str | None = None
        # DM-only: most-recent read pointer reported by the OTHER user
        # via the read_receipt field on /direct_messages. Drives the
        # ✓ Read indicator on our own bubbles. (Groups have no
        # per-member read data — we leave this None for groups.)
        self._other_read_id   : str | None = None
        self._other_read_at   : int = 0
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
        self._mention_anchor: Gtk.TextMark | None = None
        self._mention_popover: MentionPopover | None = None
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
        # Pending sends in flight: source_guid → MessageBubble. Used to
        # resolve the race between the send's HTTP response and the
        # /user/{uid} push echo of the same message — whichever arrives
        # first promotes the pending bubble to "sent" and cancels the
        # other path. Keyed by source_guid (not the temp id) because
        # the push echo carries source_guid but a different real id.
        self._pending_by_guid: dict = {}
        self._build_widgets()
        self._fetch_pinned()
        # Subscribe the DM-specific Faye channel so typing pulses for
        # this conversation start arriving. Group typing rides
        # /group/{gid} which we already subscribe at app start.
        if self._is_dm:
            push = getattr(self._win, "_push", None)
            if push is not None:
                push.subscribe_dm(self._dm_channel_key())

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

        self._mic_btn = Gtk.Button(icon_name="audio-input-microphone-symbolic")
        self._mic_btn.add_css_class("flat")
        self._mic_btn.add_css_class("compose-btn")
        self._mic_btn.set_tooltip_text("Record voice message")
        self._mic_btn.set_valign(Gtk.Align.END)
        self._mic_btn.connect("clicked", self._toggle_recording)
        self._recording_pipeline = None
        self._record_path = None
        compose.append(self._mic_btn)

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

        # Typing-indicator outbound pulses (groups + DMs both publish).
        self._entry.get_buffer().connect(
            "changed", self._on_buf_changed_typing)

        # ── @-mention autocomplete (groups only) ──
        # The buffer-changed handler is wired up unconditionally so that
        # `@` keystrokes are still recognized after the (async) member
        # fetch completes — see _ensure_mention_popover.
        if not self._is_dm:
            self._entry.get_buffer().connect(
                "changed", self._on_buf_changed)
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

    # ── Lifecycle ──────────────────────────────────────────────────
    def stop(self):
        """Stop fallback DM poll timer (push lives in BanterWindow)."""
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
            try:
                GLib.source_remove(self._typing_clear_id)
            except Exception:
                pass
            self._typing_clear_id = 0
        # Bumping the seq invalidates any in-flight pin timeouts so they
        # short-circuit instead of touching a destroyed adjustment.
        self._pin_seq += 1
        if self._mention_popover is not None:
            self._mention_popover.unparent()
            self._mention_popover = None
            self._mention_anchor = None

    # ── Conversation id (shared by attachments / read-receipts / voice) ──
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
