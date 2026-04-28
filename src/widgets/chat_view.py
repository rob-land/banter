"""Banter — ChatView: message list + compose bar."""

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
        # For DMs: the other participant's user_id (used for fetch/send)
        self._other_uid    = str(other_user_id) if other_user_id else self._gid
        self._poll_ms      = self._read_poll_interval()
        self._oldest_id   = None
        self._newest_id   = None
        self._oldest_date = None
        self._newest_date = None
        self._loading     = False
        self._pending_img_url = None
        self._poll_id    = None
        # Maps message id → MessageBubble widget for in-place refresh
        self._bubble_map : dict = {}
        # "Jump to bottom" / "new message" tracking. While the user is
        # scrolled up, _first_unread_id holds the id of the oldest
        # message that arrived since they scrolled away — clicking the
        # jump button takes them straight to it. _unread_sender holds
        # the most-recent sender's name for the button label.
        self._first_unread_id: str | None = None
        self._unread_sender:   str | None = None
        self._unread_count = 0
        self._build_widgets()

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

        # Track scroll position to decide auto-scroll vs. banner
        self._at_bottom = True
        adj = self._scroll.get_vadjustment()
        adj.connect("value-changed", self._on_scroll_changed)

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

        # ── Compose bar ──
        compose = Gtk.Box(spacing=8)
        compose.add_css_class("compose-bar")

        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic")
        attach_btn.add_css_class("flat")
        attach_btn.add_css_class("compose-btn")
        attach_btn.set_tooltip_text("Attach image")
        attach_btn.set_valign(Gtk.Align.END)
        attach_btn.connect("clicked", self._pick_image)
        compose.append(attach_btn)

        self._entry = Gtk.TextView()
        self._entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._entry.set_accepts_tab(False)
        self._entry.set_pixels_above_lines(4)
        self._entry.set_pixels_below_lines(4)
        self._entry.get_buffer().set_text("")

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

        # ── Assemble via Adw.ToolbarView so the compose + preview bars
        #     sit in bottom-bar slots that the compositor treats as
        #     keyboard-avoiding safe areas. On Phosh/squeekboard this
        #     causes the window content to shrink when the OSK opens so
        #     the compose bar stays visible above the keyboard. ──
        tv = Adw.ToolbarView()
        tv.set_vexpand(True)
        tv.set_content(scroll_overlay)
        # add_bottom_bar stacks in order added, so preview sits above compose
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
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

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

        if ev_type == "line.create":
            if str(subject.get("group_id", "")) == self._gid:
                msg_id = str(subject.get("id", ""))
                if msg_id and msg_id in self._bubble_map:
                    # Already displayed (we sent it ourselves or received a duplicate)
                    # — refresh reactions in case server data differs
                    dbg("push: line.create duplicate ignored for msg %s", msg_id)
                    self._bubble_map[msg_id].refresh(subject)
                else:
                    self._append_new([subject])

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
                self._bubble_map[msg_id].refresh_from_server()()
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
        self._bubble_map[str(msg["id"])] = bubble
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

    def _on_scroll_changed(self, adj):
        """Track whether the user is at the bottom of the message list."""
        at_bottom = adj.get_value() >= (adj.get_upper() - adj.get_page_size() - 40)
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
        """Scroll to the bottom of the message list.
        Deferred one frame so GTK has laid out any newly-appended widgets."""
        def _do_scroll():
            adj = self._scroll.get_vadjustment()
            adj.set_value(adj.get_upper())
            return False
        GLib.idle_add(_do_scroll)

    # ── Sending ──
    def _on_key(self, ctrl, keyval, keycode, state):
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Return and not shift:
            self._send()
            return True
        return False

    def _send(self, *_):
        buf  = self._entry.get_buffer()
        text = buf.get_text(
            buf.get_start_iter(), buf.get_end_iter(), False
        ).strip()

        if not text and not self._pending_img_url:
            return

        buf.set_text("")
        atts = []
        if self._pending_img_url:
            atts.append({"type": "image", "url": self._pending_img_url})
            self._clear_attachment()

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

    # ── Image attach ──
    def _pick_image(self, *_):
        fd = Gtk.FileDialog()
        fd.set_title("Attach Image")

        ff = Gtk.FileFilter()
        ff.set_name("Images")
        for mime in ("image/jpeg", "image/png",
                     "image/gif", "image/webp"):
            ff.add_mime_type(mime)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(ff)
        fd.set_filters(store)
        fd.open(self._win, None, self._on_image_picked)

    def _on_image_picked(self, fd, result):
        try:
            f    = fd.open_finish(result)
            path = f.get_path()
        except GLib.Error:
            return

        self._win.toast("Uploading image…")

        def worker():
            url = self._api.upload_image(path)
            if url:
                GLib.idle_add(self._set_pending, url,
                               Path(path).name)
            else:
                GLib.idle_add(lambda: self._win.toast(
                    "Image upload failed"))

        run_in_background(worker)

    def _set_pending(self, url, name):
        self._pending_img_url = url
        self._preview_label.set_text(name)
        self._preview_bar.set_visible(True)
        self._win.toast("Image ready – press send")

    def _clear_attachment(self, *_):
        self._pending_img_url = None
        self._preview_bar.set_visible(False)


