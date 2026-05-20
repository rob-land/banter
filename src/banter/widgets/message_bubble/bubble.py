"""MessageBubble — the assembled widget. Layout-building and the small
action methods that don't fit into a feature mixin live here; the
heavier feature areas (reactions, save actions, status indicators,
context menu) are pulled in via mixins from sibling modules.
"""

from datetime import datetime

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ...api import GroupMeAPI
from ...async_utils import run_in_background
from ...constants import esc
from ...helpers import set_avatar_from_url
from ..album_card import AlbumCard
from ..event_card import EventCard
from ..misc import FileAttachment, ImageAttachment, VideoAttachment, VoiceAttachment
from ..poll_card import PollCard
from ._context_menu import ContextMenuMixin
from ._reactions import ReactionsMixin
from ._save_actions import SaveActionsMixin
from ._status import StatusMixin
from ._text import _build_text_markup


class MessageBubble(
    StatusMixin,
    ReactionsMixin,
    SaveActionsMixin,
    ContextMenuMixin,
    Gtk.Box,
):
    # Layout tunables — class-level so a future style pass can adjust
    # them in one place rather than chasing literals through the file.
    MAX_TEXT_CHARS  = 45    # main message body + location label wrap width
    MAX_NAME_CHARS  = 28    # sender header + reply-quote sender ellipsize cap
    MAX_QUOTE_CHARS = 40    # reply quote text wrap width
    MAX_QUOTE_LEN   = 120   # reply quote text truncated character count
    MIN_BUBBLE_GAP  = 32    # px — minimum spacer width opposite the bubble

    def __init__(self, msg: dict, me_id, group_id: str,
                 api: GroupMeAPI, window, *, pending: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.msg  = msg
        self.gid  = group_id
        self.api  = api
        self.win  = window
        self.me   = str(me_id)
        is_mine    = str(msg.get("user_id", "")) == self.me
        self.is_mine = is_mine
        # Pending/failed state for optimistic-UI sends. A `pending`
        # bubble is rendered grayed-out with a "Sending…" caption and
        # has no reactions row or context menu — the message hasn't
        # been confirmed by the server yet, so we can't react to it,
        # edit it, or delete it. transition_to_sent() and
        # transition_to_failed() flip the state once the API call
        # returns. transition_to_pending() is used for retries.
        self.is_pending = bool(pending)
        self.is_failed  = False
        # Labels with set_selectable(True) — checked on right-click to
        # decide whether to show our context menu or the built-in
        # text-selection menu (cut/copy/paste).
        self._selectable_labels: list = []
        # Pin indicator widget (hidden by default) — toggled by
        # `set_pinned()` whenever the chat view's pinned set changes.
        self._pin_icon = self._make_pin_icon()
        # DM "Read" indicator for own bubbles — toggled by `set_read()`
        # when ChatView's known DM read_receipt advances past this
        # message's id. Hidden by default.
        self._read_icon = self._make_read_icon()

        # Cache this sender's name so reaction tooltips can resolve it
        uid  = str(msg.get("user_id", ""))
        name = msg.get("name", "")
        if uid and name:
            window.cache_sender_name(uid, name)

        # ── Sender header (others only) ──
        if not is_mine:
            hdr = Gtk.Box(spacing=6)
            av  = Adw.Avatar(size=28,
                             text=esc(msg.get("name", "?")),
                             show_initials=True)
            set_avatar_from_url(av, msg.get("avatar_url"))
            hdr.append(av)

            nm = Gtk.Label(label=esc(msg.get("name", "Unknown")))
            nm.add_css_class("dim-caption")
            nm.add_css_class("bold-name")
            nm.set_halign(Gtk.Align.START)
            nm.set_ellipsize(Pango.EllipsizeMode.END)
            nm.set_max_width_chars(self.MAX_NAME_CHARS)
            hdr.append(nm)

            ts  = msg.get("created_at", 0)
            dt  = datetime.fromtimestamp(ts)
            tl  = Gtk.Label(label=dt.strftime("%-I:%M %p"))
            tl.add_css_class("dim-caption")
            tl.add_css_class("dim-label")
            tl.set_margin_start(4)
            hdr.append(tl)

            self._edited_lbl = self._make_edited_label(msg)
            if self._edited_lbl is not None:
                hdr.append(self._edited_lbl)

            hdr.append(self._pin_icon)

            hdr.set_margin_start(4)

            # Click avatar (only) → open the contact detail dialog.
            # Restricting to the avatar stops accidental taps while
            # scrolling a group chat on a phone.
            sender_uid  = str(msg.get("user_id", ""))
            sender_info = {
                "name"      : msg.get("name", ""),
                "avatar_url": msg.get("avatar_url", ""),
                "user_id"   : sender_uid,
            }
            av.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            gest = Gtk.GestureClick()
            gest.connect("pressed",
                         lambda *_, u=sender_uid, s=sender_info:
                             window.open_contact_detail(s, u))
            av.add_controller(gest)
            self.append(hdr)

        # ── Bubble ──
        # GTK4 wrapping fix: labels propagate their natural (unwrapped) width
        # upward, expanding the window. The correct pattern is:
        #   1. A row_box fills the full width (hexpand=True, no shrink)
        #   2. An expanding spacer pushes the bubble to the correct side
        #   3. The bubble itself does NOT hexpand — it sizes to content
        #   4. lbl.set_width_request(1) breaks the natural-size feedback loop
        #      so GTK stops asking the label how wide it wants to be and
        #      instead wraps it to whatever width the bubble is allocated.
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row_box.set_hexpand(True)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        # 32 px minimum gap so bubble never runs edge-to-edge
        spacer.set_size_request(self.MIN_BUBBLE_GAP, -1)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bubble.add_css_class("msg-bubble")
        bubble.add_css_class("mine" if is_mine else "theirs")
        # bubble does NOT hexpand — it must be constrained by the spacer

        # ── Reply quote block (async) ──────────────────────────────────
        # Detect a reply attachment and reserve a placeholder that fills in
        # once the parent message is fetched from the API.
        reply_att = next(
            (a for a in msg.get("attachments", []) if a.get("type") == "reply"),
            None,
        )
        if reply_att:
            parent_id = str(reply_att.get("reply_id") or
                            reply_att.get("base_reply_id") or "")
            if parent_id:
                # Placeholder shown while loading
                self._quote_box = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL, spacing=2)
                self._quote_box.add_css_class("reply-quote")
                placeholder = Gtk.Label(label="↩  loading…")
                placeholder.add_css_class("reply-quote-text")
                placeholder.set_xalign(0)
                self._quote_box.append(placeholder)
                bubble.append(self._quote_box)

                # Fetch the parent message asynchronously
                gid = group_id

                def _fetch_parent(pid=parent_id, gid=gid):
                    msgs = api.get_messages(gid,
                                            before_id=str(int(pid) + 1),
                                            limit=1)
                    parent = next(
                        (m for m in msgs if str(m.get("id")) == pid), None)
                    GLib.idle_add(self._set_quote, parent)
                run_in_background(_fetch_parent)

        text = (msg.get("text") or "").strip()

        # Voice notes carry a server-injected downgrade warning in
        # `text` ("⚠️You received a voice note. Please update to the
        # latest version of GroupMe to view/respond.") meant for clients
        # that don't render `audio` attachments. We do — so swallow the
        # text entirely. Skipping any message that has an audio
        # attachment is safer than substring-matching the warning,
        # which gets translated by GroupMe and would silently break.
        if any(a.get("type") == "audio"
               for a in msg.get("attachments", [])):
            text = ""

        # Pre-scan attachments for a pack-emoji entry; if present, the
        # text needs to render as an inline mix of labels and images
        # rather than a single Label.
        emoji_att = next(
            (a for a in msg.get("attachments", []) if a.get("type") == "emoji"),
            None,
        )
        used_mixed_text = False
        if text and emoji_att:
            placeholder = emoji_att.get("placeholder") or ""
            charmap     = emoji_att.get("charmap") or []
            if placeholder and charmap:
                mixed = self._build_pack_emoji_row(text, placeholder, charmap)
                if mixed is not None:
                    bubble.append(mixed)
                    used_mixed_text = True

        if text and not used_mixed_text:
            mentions_att = next(
                (a for a in msg.get("attachments", [])
                 if a.get("type") == "mentions"),
                None,
            )
            markup, use_markup = _build_text_markup(
                text, mentions_att, is_mine=is_mine)
            lbl = Gtk.Label(wrap=True)
            lbl.set_xalign(0)
            lbl.set_selectable(True)
            self._selectable_labels.append(lbl)
            lbl.set_max_width_chars(self.MAX_TEXT_CHARS)
            # WORD_CHAR wrapping lets long URLs/unbreakable tokens break
            # mid-string instead of forcing the label to demand the full
            # content width. This is what prevents the "header disappears"
            # bug without collapsing the bubble to 1px.
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if use_markup:
                # Use markup so <a href> links are rendered as clickable
                # and mention runs are highlighted.
                lbl.set_markup(markup)
                lbl.set_use_markup(True)
            else:
                lbl.set_text(text)
            bubble.append(lbl)

        for att in msg.get("attachments", []):
            kind = att.get("type", "")
            if kind in ("reply", "emoji"):
                continue   # reply → quote block; emoji → handled above
            if kind == "image":
                img = ImageAttachment(att["url"], window)
                bubble.append(img)
            elif kind == "video":
                url = att.get("url", "")
                if url:
                    bubble.append(VideoAttachment(
                        url, att.get("preview_url", ""), window))
            elif kind == "file":
                fid = att.get("file_id")
                if fid:
                    # FileAttachment hits file.groupme.com which uses
                    # the conversation_id (gid for groups, <lo>+<hi>
                    # for DMs), not the bubble's display gid.
                    bubble.append(FileAttachment(
                        fid, self._conversation_id(), api, window))
            elif kind == "audio":
                # Voice notes (Android/iOS "voice memo"). Hosted at
                # m.groupme.com with a 24h-signed Azure CDN redirect;
                # auth is via Cookie, not query string. See
                # `api.download_audio`.
                url = att.get("url", "")
                if url:
                    bubble.append(VoiceAttachment(
                        url, att.get("duration"),
                        att.get("peaks"), api, window))
            elif kind == "location":
                loc = Gtk.Label(
                    label=f"📍 {att.get('name','Location')}"
                          f"\n{att.get('lat')}, {att.get('lng')}")
                loc.set_wrap(True)
                loc.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                loc.set_max_width_chars(self.MAX_TEXT_CHARS)
                loc.set_xalign(0)
                bubble.append(loc)
            elif kind == "split":
                lbl = Gtk.Label(label="💳 Split request")
                bubble.append(lbl)
            elif kind.startswith("event"):
                # GroupMe shares the "event" attachment across several
                # system messages — creation, "is going", "is not going",
                # updates. Only the "created event" post should render a
                # card; the RSVP follow-ups would otherwise duplicate a
                # card for every response, cluttering the chat.
                if "created event" in text.lower():
                    event_id   = att.get("event_id") or att.get("id")
                    event_data = att.get("event") if isinstance(att.get("event"), dict) else None
                    if event_id or event_data:
                        card = EventCard(api, group_id, me_id,
                                         event_id, event_data, window)
                        bubble.append(card)
            elif kind == "poll":
                poll_id = att.get("poll_id") or att.get("id")
                if poll_id:
                    bubble.append(PollCard(
                        api, group_id, me_id, poll_id, window))

        # Album-create / add-media events live on `msg.event`, NOT
        # in attachments — separate dispatch from the loop above.
        # We render only on `gallery.album.create`; the `.add.media`
        # follow-up has the same data shape but firing a card on
        # every "added N photos" message would clutter the chat.
        ev = msg.get("event") or {}
        if ev.get("type") == "gallery.album.create":
            album_data = (ev.get("data") or {}).get("album") or {}
            if album_data.get("album_id"):
                bubble.append(AlbumCard(
                    api, group_id, album_data, window))

        # Timestamp (mine only, shown inside bubble)
        self._ts_box = None
        if is_mine:
            self._ts_box = Gtk.Box(spacing=4)
            self._ts_box.set_halign(Gtk.Align.END)
            self._ts_box.append(self._pin_icon)
            self._edited_lbl = self._make_edited_label(msg)
            if self._edited_lbl is not None:
                self._ts_box.append(self._edited_lbl)
            ts = msg.get("created_at", 0)
            tl = Gtk.Label(
                label=datetime.fromtimestamp(ts).strftime("%-I:%M %p"))
            tl.add_css_class("dim-caption")
            self._ts_box.append(tl)
            # Read indicator pinned at the end of the timestamp row so
            # the order reads "{pin?} {edited?} 12:34 PM ✓".
            self._ts_box.append(self._read_icon)
            bubble.append(self._ts_box)

        if is_mine:
            row_box.append(spacer)
            row_box.append(bubble)
        else:
            row_box.append(bubble)
            row_box.append(spacer)
        self.append(row_box)

        # Stash bubble + row_box references so transition_to_sent /
        # transition_to_failed can mutate them later (add status
        # indicators, build the reactions row that was deferred for
        # pending bubbles, etc.).
        self._bubble_inner = bubble
        self._bubble_for_menu = bubble
        self._row_box   = row_box
        self._spacer    = spacer

        # ── Reactions row ──
        # Skip for pending bubbles — there's nothing to react to until
        # the server has assigned a real id. transition_to_sent will
        # build it lazily.
        self._reactions_box = None
        if not self.is_pending:
            self._build_reactions_row()
            self._render_reactions(msg.get("reactions", []),
                                   msg.get("favorited_by", []))

        # ── Pending/failed indicator ──
        # Inserted alongside the timestamp for own messages. Includes a
        # small spinner during sending and a Retry/Discard action row
        # below the bubble in the failed state.
        self._pending_status_lbl = None
        self._pending_spinner    = None
        self._action_row         = None
        if self.is_pending:
            bubble.add_css_class("pending")
            self._add_pending_indicator()

        self.set_margin_bottom(6)
        self.set_margin_start(8)
        self.set_margin_end(8)

        # ── Context menu (right-click + long-press) ────────────────────
        # Skipped for pending bubbles: there's no message id yet so
        # reply / copy / edit / delete / pin all have nothing to
        # operate on. Re-installed by transition_to_sent.
        # The right-click gesture is in CAPTURE phase so it pre-empts
        # the built-in context menu of any selectable child label —
        # but the handler bows out (lets the default through) when
        # text is currently selected, so the user can still right-click
        # to copy a selected substring.
        if not self.is_pending:
            self._wire_context_menu()

    # ── Reply quote ──
    def _set_quote(self, parent_msg):
        """Fill in the reply quote block once the parent message is loaded."""
        if not hasattr(self, "_quote_box"):
            return
        # Clear the placeholder
        child = self._quote_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._quote_box.remove(child)
            child = nxt

        if not parent_msg:
            lbl = Gtk.Label(label="↩  Original message unavailable")
            lbl.add_css_class("reply-quote-text")
            lbl.set_xalign(0)
            self._quote_box.append(lbl)
            return

        # Sender name
        name_lbl = Gtk.Label(label=esc(parent_msg.get("name", "Unknown")))
        name_lbl.add_css_class("reply-quote-name")
        name_lbl.set_xalign(0)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_lbl.set_max_width_chars(self.MAX_NAME_CHARS)
        self._quote_box.append(name_lbl)

        # Message preview (first 120 chars)
        parent_text = (parent_msg.get("text") or "").strip()
        if not parent_text:
            # Image or attachment with no text
            atts = parent_msg.get("attachments", [])
            if any(a.get("type") == "image" for a in atts):
                parent_text = "📷 Image"
            elif atts:
                parent_text = "📎 Attachment"
            else:
                parent_text = "…"

        preview = (parent_text[:self.MAX_QUOTE_LEN] +
                   ("…" if len(parent_text) > self.MAX_QUOTE_LEN else ""))
        text_lbl = Gtk.Label(label=preview)
        text_lbl.add_css_class("reply-quote-text")
        text_lbl.set_xalign(0)
        text_lbl.set_wrap(True)
        text_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text_lbl.set_max_width_chars(self.MAX_QUOTE_CHARS)
        self._quote_box.append(text_lbl)

    # ── Refresh / edited indicator / text replacement ──
    def refresh_from_server(self):
        """Re-fetch this single message from the API and re-render reactions."""
        gid = self.gid
        mid = self.msg["id"]

        def worker():
            msgs = self.api.get_messages(gid, before_id=str(int(mid) + 1), limit=1)
            for m in msgs:
                if str(m.get("id")) == str(mid):
                    GLib.idle_add(self.refresh, m)
                    return
        run_in_background(worker)

    def refresh(self, updated_msg: dict):
        """Update reactions in-place from a freshly-fetched server message."""
        self.msg = updated_msg
        self._render_reactions(
            updated_msg.get("reactions", []),
            updated_msg.get("favorited_by", [])
        )

    @staticmethod
    def _is_edited(msg: dict) -> bool:
        """Return True if `msg` has been edited at least once. GroupMe
        sets `updated_at` to the edit timestamp; for unedited messages
        it equals or is missing relative to `created_at`."""
        try:
            updated = int(msg.get("updated_at") or 0)
            created = int(msg.get("created_at") or 0)
        except (TypeError, ValueError):
            return False
        # Allow 1-second jitter — GroupMe sometimes sets updated_at a
        # second after created_at on send for unedited messages.
        return updated > 0 and updated > created + 1

    def _make_edited_label(self, msg: dict):
        """Return a 'edited HH:MM' tag label if the message has been
        edited, or None otherwise. Hover-tooltip shows the full date."""
        if not self._is_edited(msg):
            return None
        try:
            updated = int(msg.get("updated_at") or 0)
            dt = datetime.fromtimestamp(updated)
        except (TypeError, ValueError):
            return None
        lbl = Gtk.Label(label=f"edited {dt.strftime('%-I:%M %p')}")
        lbl.add_css_class("dim-caption")
        lbl.add_css_class("dim-label")
        lbl.set_tooltip_text(
            f"Last edited {dt.strftime('%a %b %-d, %-I:%M %p')}")
        return lbl

    def update_text_from(self, msg: dict):
        """Replace the visible message text and edited-indicator from
        a freshly-edited message dict. Called by ChatView when a
        line.update push event arrives."""
        self.msg.update(msg)
        # Find and replace the existing text label inside the bubble.
        # Cheaper to rebuild the label than to reach into pango.
        bubble = self._bubble_for_menu
        if bubble is None:
            return
        new_text = (msg.get("text") or "").strip()

        # Replace any existing _selectable_labels[0] (the main text)
        # with a fresh one. Other selectable labels (reply quote) are
        # left intact.
        old = self._selectable_labels[0] if self._selectable_labels else None
        if old is not None and old.get_parent() is bubble:
            mentions_att = next(
                (a for a in self.msg.get("attachments", [])
                 if a.get("type") == "mentions"),
                None,
            )
            markup, use_markup = _build_text_markup(
                new_text, mentions_att, is_mine=self.is_mine)
            if use_markup:
                old.set_markup(markup)
            else:
                old.set_text(new_text)

        # Refresh the "(edited)" tag in-place.
        new_lbl = self._make_edited_label(msg)
        if hasattr(self, "_edited_lbl") and self._edited_lbl is not None:
            parent = self._edited_lbl.get_parent()
            if parent is not None:
                parent.remove(self._edited_lbl)
            self._edited_lbl = None
        if new_lbl is not None:
            # Insert into the same box as the timestamp — that varies
            # for own vs others' messages. Walk the bubble's first
            # children looking for the timestamp's parent.
            self._edited_lbl = new_lbl
            # Best-effort: append to the bubble; visual ordering is
            # lost but the indicator is still visible.
            bubble.append(new_lbl)

    # ── Pin / unpin / reply / copy / edit / delete actions ──
    def _action_pin(self, *_):
        self._do_pin(True)

    def _action_unpin(self, *_):
        self._do_pin(False)

    def _do_pin(self, pin: bool):
        conv_id = self._conversation_id()
        mid     = str(self.msg.get("id"))
        api     = self.api
        win     = self.win

        def worker():
            return (api.pin_message(conv_id, mid) if pin
                    else api.unpin_message(conv_id, mid))

        def on_done(ok):
            if ok:
                cv = getattr(win, "_chat_view", None)
                if cv is not None:
                    cv.mark_pinned(mid, pin)
                try:
                    win.toast("Message pinned" if pin else "Message unpinned")
                except Exception:
                    pass
            else:
                try:
                    win.toast("Failed to pin message" if pin
                              else "Failed to unpin message")
                except Exception:
                    pass

        run_in_background(worker, on_done)

    def _action_reply(self, *_):
        chat_view = getattr(self.win, "_chat_view", None)
        if chat_view is not None:
            chat_view.set_reply_target(self.msg)

    def _action_copy(self, *_):
        text = self.msg.get("text") or ""
        if not text:
            return
        clipboard = self.get_clipboard()
        clipboard.set(text)
        try:
            self.win.toast("Copied to clipboard")
        except Exception:
            pass

    def _action_edit(self, *_):
        # Late import to keep the dialog cost off the bubble import
        # path; instances are constructed once per right-click.
        from .edit_dialog import EditMessageDialog
        EditMessageDialog(self).present(self.win)

    def _action_delete(self, *_):
        msg = Adw.MessageDialog(
            transient_for=self.win,
            heading="Delete message?",
            body="This cannot be undone.",
        )
        msg.add_response("cancel", "Cancel")
        msg.add_response("delete", "Delete")
        msg.set_response_appearance(
            "delete", Adw.ResponseAppearance.DESTRUCTIVE)
        msg.set_default_response("cancel")
        msg.set_close_response("cancel")

        def on_response(_dlg, resp):
            if resp != "delete":
                return
            self._do_delete()
        msg.connect("response", on_response)
        msg.present()

    def _do_delete(self):
        conv_id = self._conversation_id()
        mid     = str(self.msg.get("id"))

        def worker():
            return self.api.delete_message(conv_id, mid)

        def on_done(ok):
            if ok:
                # Remove from chat view's bubble map and from the box
                cv = getattr(self.win, "_chat_view", None)
                if cv is not None and mid in getattr(cv, "_bubble_map", {}):
                    cv._bubble_map.pop(mid, None)
                parent = self.get_parent()
                if parent is not None:
                    parent.remove(self)
                try:
                    self.win.toast("Message deleted")
                except Exception:
                    pass
            else:
                try:
                    self.win.toast("Failed to delete message")
                except Exception:
                    pass

        run_in_background(worker, on_done)

    def _conversation_id(self) -> str:
        """Compute the API conversation_id for edit/delete. For groups
        this is the group_id; for DMs GroupMe uses '<a>+<b>' with the
        two participant ids sorted as integers (smaller first)."""
        cv = getattr(self.win, "_chat_view", None)
        is_dm = bool(cv and getattr(cv, "_is_dm", False))
        if not is_dm:
            return str(self.gid)
        try:
            a = int(self.me)
            b = int(getattr(cv, "_other_uid", 0) or 0)
        except (TypeError, ValueError):
            return f"{self.me}+{getattr(cv, '_other_uid', '')}"
        lo, hi = (a, b) if a < b else (b, a)
        return f"{lo}+{hi}"
