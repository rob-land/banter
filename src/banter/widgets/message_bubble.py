"""Banter — MessageBubble widget with inline reactions."""

import re
import urllib.request
from datetime import datetime
from pathlib import Path
from gi.repository import Gtk, Adw, GLib, Gdk, Gio, Pango

from ..constants import DEBUG, esc, EMOJI_LOG
from ..async_utils import run_in_background
from ..api import GroupMeAPI
from ..constants import CACHE_DIR
from ..helpers import set_avatar_from_url, set_pack_emoji, _cache_key
from .misc import ImageAttachment, VideoAttachment, VoiceAttachment, FileAttachment
from .event_card import EventCard
from .album_card import AlbumCard
from .poll_card import PollCard

# ── URL / email linkification ─────────────────────────────────────────
_URL_RE = re.compile(
    r"(https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+|[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)


def _linkify(text: str) -> tuple:
    """Return (pango_markup, has_links).
    Wraps URLs and email addresses in <a href="..."> tags."""
    parts = _URL_RE.split(text)
    if len(parts) == 1:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), False)

    out = []
    for i, part in enumerate(parts):
        safe = part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if i % 2 == 0:
            out.append(safe)
        else:
            if part.lower().startswith("www."):
                href = "http://" + part
            elif "@" in part and not part.lower().startswith("http"):
                href = "mailto:" + part
            else:
                href = part
            href_safe = href.replace("&", "&amp;").replace('"', "&quot;")
            out.append(f'<a href="{href_safe}">{safe}</a>')
    return ("".join(out), True)


def _mention_ranges(mentions_att, text_len):
    """Return a sorted, merged list of (start, end) for the mention loci
    in `mentions_att`, clamped to `text_len`. Defensive against bad
    input."""
    if not mentions_att:
        return []
    raw = mentions_att.get("loci") or []
    out = []
    for entry in raw:
        try:
            s, l = int(entry[0]), int(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if l <= 0 or s < 0 or s >= text_len:
            continue
        out.append((s, min(s + l, text_len)))
    out.sort()
    # Merge overlaps so we don't emit nested <span> tags
    merged = []
    for r in out:
        if merged and r[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], r[1]))
        else:
            merged.append(r)
    return merged


def _accent_hex() -> str:
    """Current libadwaita accent color as #RRGGBB. Pango markup can't
    reference @accent_color directly, so we resolve it at render time
    and inject the literal hex — keeps mentions in sync with the user's
    theme accent across light/dark and accent changes."""
    try:
        rgba = Adw.StyleManager.get_default().get_accent_color_rgba()
        r = int(rgba.red   * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue  * 255)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        # libadwaita < 1.6: fall back to the GNOME default blue
        return "#3584e4"


def _build_text_markup(text: str, mentions_att, is_mine: bool = False) -> tuple:
    """Return (pango_markup, use_markup) for `text`, applying both URL
    linkification and mention highlighting. Mention spans are rendered
    as bold runs; URLs in non-mention regions are still turned into
    clickable links.

    On outgoing (own) bubbles, the bubble background is the libadwaita
    accent color — the same blue we'd use for mentions on other
    bubbles. So we render mentions on our own bubbles in white +
    underline to keep them visible against the accent background."""
    ranges = _mention_ranges(mentions_att, len(text))
    if not ranges:
        return _linkify(text)

    if is_mine:
        open_tag = '<span weight="bold" underline="single">'
    else:
        open_tag = f'<span weight="bold" foreground="{_accent_hex()}">'

    parts  = []
    cursor = 0
    for s, e in ranges:
        if cursor < s:
            seg_markup, _ = _linkify(text[cursor:s])
            parts.append(seg_markup)
        mention_text = text[s:e]
        escaped = (mention_text
                   .replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;"))
        parts.append(f'{open_tag}{escaped}</span>')
        cursor = e
    if cursor < len(text):
        seg_markup, _ = _linkify(text[cursor:])
        parts.append(seg_markup)
    return ("".join(parts), True)


class MessageBubble(Gtk.Box):
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

    # ── Reactions ──
    def _build_reactions_row(self):
        """Create and append the reactions Gtk.Box. Idempotent — no-op
        if the row already exists (e.g. transition_to_sent re-calling
        on an already-built bubble)."""
        if self._reactions_box is not None:
            return
        self._reactions_box = Gtk.Box(spacing=4)
        self._reactions_box.set_halign(
            Gtk.Align.END if self.is_mine else Gtk.Align.START)
        self._reactions_box.set_margin_start(8)
        self._reactions_box.set_margin_end(8)
        self._reactions_box.set_margin_top(2)
        self.append(self._reactions_box)

    def _wire_context_menu(self):
        """Attach right-click + long-press gesture handlers to the
        inner bubble. Idempotent — repeated wiring would just stack
        gestures harmlessly, but skip if already done."""
        if getattr(self, "_menu_wired", False):
            return
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        rclick.connect("pressed", self._on_menu_click)
        self._bubble_for_menu.add_controller(rclick)
        long_press = Gtk.GestureLongPress()
        long_press.set_touch_only(False)
        long_press.connect("pressed", self._on_menu_long_press)
        self._bubble_for_menu.add_controller(long_press)
        self._menu_wired = True

    # ── Pending / failed state ─────────────────────────────────────
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

    def _render_reactions(self, reactions: list, favorited_by: list):
        """Rebuild the reactions row from server data.

        All interactions — adding, removing, switching, seeing reactors —
        are consolidated into a single ReactionsSheet. Clicking either a
        pill or the add-reaction button opens the sheet. This avoids the
        old mix of tooltip / tap-pill-to-switch / tap-empty-row-to-inspect
        affordances that didn't translate to touch."""
        if self._reactions_box is None:
            return   # pending bubble — nothing to render
        # Clear
        child = self._reactions_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._reactions_box.remove(child)
            child = nxt

        # Build reaction_map: unique key → metadata.
        # For pack reactions, key includes pack_id+offset so multiple
        # distinct pack emojis don't collapse into a single pill.
        reaction_map = {}
        for r in (reactions or []):
            r_type = r.get("type", "unicode")
            if r_type == "emoji":
                pack_id = r.get("pack_id")
                # GroupMe responses use either `offset` or `pack_index`
                # depending on endpoint/era — accept both.
                offset  = r.get("offset")
                if offset is None:
                    offset = r.get("pack_index")
                key     = f"pack:{pack_id}:{offset}"
                entry = {
                    "uids"     : set(),
                    "raw_code" : r.get("placeholder") or "😊",
                    "is_pack"  : True,
                    "pack_id"  : pack_id,
                    "offset"   : offset,
                }
                # Capture pack reaction shapes for schema investigation.
                # Gated on DEBUG so a chat with many pack reactions
                # doesn't do synchronous disk writes on every re-render.
                if DEBUG:
                    try:
                        with open(EMOJI_LOG, "a") as f:
                            f.write(f"{datetime.now().isoformat()}  msg={self.msg.get('id')}  {r}\n")
                    except Exception:
                        pass
            else:
                code = r.get("code") or "❤️"
                key  = code
                entry = {
                    "uids"     : set(),
                    "raw_code" : code,
                    "is_pack"  : False,
                }
            uid_list = r.get("user_ids", [])
            if key not in reaction_map:
                reaction_map[key] = entry
            reaction_map[key]["uids"].update(str(u) for u in uid_list)

        # Legacy heart likes fallback
        if favorited_by and not reaction_map:
            uids = set(str(u) for u in favorited_by)
            reaction_map["❤️"] = {"uids": uids, "raw_code": "❤️", "is_pack": False}

        me = self.me

        # Reaction pills
        for _, info in reaction_map.items():
            uid_set = info["uids"]
            count   = len(uid_set)
            i_used  = me in uid_set

            pill = Gtk.Button()
            pill.add_css_class("flat")
            pill.add_css_class("reaction-pill")
            if i_used:
                pill.add_css_class("reaction-pill-mine")

            pill_content = Gtk.Box(spacing=4)
            if info.get("is_pack"):
                emoji_widget = Gtk.Image()
                emoji_widget.set_pixel_size(18)
                pid, off = info.get("pack_id"), info.get("offset")
                if pid is not None and off is not None:
                    set_pack_emoji(emoji_widget, pid, off, 18)
                pill_content.append(emoji_widget)
            else:
                emoji_lbl = Gtk.Label(label=info.get("raw_code", ""))
                pill_content.append(emoji_lbl)
            count_lbl = Gtk.Label(label=str(count))
            pill_content.append(count_lbl)
            pill.set_child(pill_content)

            pill.connect("clicked",
                         lambda *_, m=reaction_map: self._open_reactions_sheet(m))
            self._reactions_box.append(pill)

        # Add-reaction button — always shown so the user can change or
        # add new reactions even when they've already reacted.
        add_btn = self._make_add_reaction_btn()
        add_btn.connect("clicked",
                        lambda *_, m=reaction_map: self._open_reactions_sheet(m))
        self._reactions_box.append(add_btn)

    def _open_reactions_sheet(self, reaction_map: dict):
        try:
            from .reactions_sheet import ReactionsSheet
            sheet = ReactionsSheet(self, reaction_map)
            sheet.present(self.win)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            # Always print to stderr so the user running from a terminal
            # can see the full stack, regardless of --debug.
            import sys
            print("=== ReactionsSheet failed ===", file=sys.stderr)
            print(tb, file=sys.stderr)
            print("=============================", file=sys.stderr)
            self.win.toast(f"Reactions unavailable: {e}")

    def _build_pack_emoji_row(self, text: str, placeholder: str, charmap: list):
        """Build an inline text+image layout for a message carrying a
        pack-emoji attachment.

        GroupMe places a placeholder unicode character in `text` at each
        position where a pack emoji should appear; `charmap` supplies
        (pack_id, offset) pairs in order. We split the text at placeholder
        occurrences and weave in Gtk.Image widgets.

        Returns a Gtk.FlowBox-based widget that wraps reasonably on narrow
        screens. For pure emoji messages (text is only placeholders) this
        is a compact row of images; for mixed messages, text and images
        flow together word-by-word."""
        if not placeholder or not charmap:
            return None

        # Split the text at placeholder boundaries, preserving the gaps.
        segments = []
        last = 0
        emoji_idx = 0
        i = 0
        while i < len(text):
            if text[i] == placeholder:
                if i > last:
                    segments.append(("text", text[last:i]))
                if emoji_idx < len(charmap):
                    pair = charmap[emoji_idx]
                    pack_id = pair[0] if len(pair) > 0 else None
                    offset  = pair[1] if len(pair) > 1 else None
                    segments.append(("emoji", pack_id, offset))
                emoji_idx += 1
                last = i + 1
            i += 1
        if last < len(text):
            segments.append(("text", text[last:]))

        # Nothing to mix — caller will fall back to the plain text label.
        if not any(s[0] == "emoji" for s in segments):
            return None

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_column_spacing(2)
        flow.set_row_spacing(2)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(64)
        flow.set_homogeneous(False)
        flow.set_halign(Gtk.Align.START)

        for seg in segments:
            if seg[0] == "text":
                # Split text segments on whitespace so the FlowBox can
                # wrap between words instead of forcing the full segment
                # onto one line.
                chunk = seg[1]
                for word in chunk.split(" "):
                    if not word:
                        continue
                    lbl = Gtk.Label(label=word)
                    lbl.set_xalign(0)
                    flow.append(lbl)
            else:
                _, pack_id, offset = seg
                img = Gtk.Image()
                img.set_pixel_size(22)
                if pack_id is not None and offset is not None:
                    set_pack_emoji(img, pack_id, offset, 22)
                flow.append(img)
        return flow

    def _make_add_reaction_btn(self):
        """Compact add-reaction button: a symbolic smiley (takes the
        label text color) next to a small '+' glyph.

        We avoid Gtk.Overlay here — it was eating click events on the
        Button in practice, leaving the control unresponsive."""
        box = Gtk.Box(spacing=1)
        box.set_valign(Gtk.Align.CENTER)

        smiley = Gtk.Image.new_from_icon_name("face-smile-symbolic")
        smiley.set_pixel_size(14)
        box.append(smiley)

        plus = Gtk.Label(label="+")
        plus.add_css_class("reaction-add-plus")
        plus.set_valign(Gtk.Align.CENTER)
        box.append(plus)

        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("reaction-add-btn")
        btn.set_child(box)
        btn.set_tooltip_text("Add reaction")
        return btn

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

    # ── Context menu ────────────────────────────────────────────────
    def _on_menu_click(self, gesture, _n_press, x, y):
        # If any selectable label currently has a selection, defer to
        # the label's built-in context menu (which has Cut/Copy/Select
        # All for the selection). Don't claim the gesture in CAPTURE
        # phase — let it fall through to the default handler.
        for lbl in self._selectable_labels:
            try:
                has_sel, _s, _e = lbl.get_selection_bounds()
            except (TypeError, ValueError):
                continue
            if has_sel:
                return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_context_menu(x, y)

    def _on_menu_long_press(self, gesture, x, y):
        self._show_context_menu(x, y)

    # GroupMe enforces a server-side time window for editing one's own
    # messages. We hide the Edit menu past it so the user doesn't see
    # a confusing API failure on a stale message. The actual server
    # window is somewhere in the ~10-15 min range; we cap at 10 min
    # to stay conservatively inside it.
    EDIT_WINDOW_SECS = 600   # 10 minutes
    EDIT_ENABLED     = True

    def _show_context_menu(self, x: float, y: float):
        menu = Gio.Menu()
        menu.append("Reply",     "bubble.reply")
        menu.append("Copy text", "bubble.copy")
        # Pin / unpin — server enforces who can do it (admins-only vs.
        # everyone is a per-group setting). We always offer the action
        # and surface a toast on failure rather than trying to mirror
        # the server-side permission state client-side.
        if self._is_pinned():
            menu.append("Unpin", "bubble.unpin")
        else:
            menu.append("Pin", "bubble.pin")
        # Save attachment items — surfaced only when the bubble carries
        # one of these. The bubble's right-click gesture is in CAPTURE
        # phase so it pre-empts the per-attachment-widget menus
        # (VideoAttachment / VoiceAttachment) — those still work in
        # contexts that don't sit inside a MessageBubble (the album
        # gallery), but inside a chat the bubble menu is the single
        # entry point.
        atts = self.msg.get("attachments") or []
        if any(a.get("type") == "image" for a in atts):
            menu.append("Save Photo…",         "bubble.save-photo")
        if any(a.get("type") == "video" for a in atts):
            menu.append("Save Video…",         "bubble.save-video")
        if any(a.get("type") == "audio" for a in atts):
            menu.append("Save Voice Message…", "bubble.save-voice")
        # `file` covers anything uploaded via the file picker — docs,
        # video files, archives, etc. Banter's own outgoing video
        # attachments land here too (only camera-shared videos use
        # type:"video"), so this is the path users hit when right-
        # clicking an mp4 they sent or received.
        if any(a.get("type") == "file" for a in atts):
            menu.append("Save Attachment…",    "bubble.save-file")
        # "Add to Album…" only makes sense in groups — Banter doesn't
        # currently expose a DM gallery, and `api.get_albums` for a
        # DM `<lo>+<hi>` conv_id is untested. Show the item only when
        # the bubble has at least one image or video attachment AND
        # we're in a group chat.
        cv = getattr(self.win, "_chat_view", None)
        in_group = bool(cv) and not getattr(cv, "_is_dm", False)
        if in_group and any(a.get("type") in ("image", "video") for a in atts):
            menu.append("Add to Album…",       "bubble.add-to-album")
        if self.is_mine:
            if self.EDIT_ENABLED:
                created = int(self.msg.get("created_at") or 0)
                now     = int(datetime.now().timestamp())
                if created and (now - created) <= self.EDIT_WINDOW_SECS:
                    menu.append("Edit", "bubble.edit")
            menu.append("Delete", "bubble.delete")

        # Action group scoped to this bubble.
        group = Gio.SimpleActionGroup()
        for name, cb in (
            ("reply",      self._action_reply),
            ("copy",       self._action_copy),
            ("pin",        self._action_pin),
            ("unpin",      self._action_unpin),
            ("edit",       self._action_edit),
            ("delete",     self._action_delete),
            ("save-photo",     self._action_save_photo),
            ("save-video",     self._action_save_video),
            ("save-voice",     self._action_save_voice),
            ("save-file",      self._action_save_file),
            ("add-to-album",   self._action_add_to_album),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            group.add_action(act)
        self._bubble_for_menu.insert_action_group("bubble", group)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self._bubble_for_menu)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.set_has_arrow(False)
        popover.popup()

    # ── Pin indicator ─────────────────────────────────────────────────
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

    # ── DM "Read" indicator ──────────────────────────────────────────
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

    # ── Save attachment actions ────────────────────────────────────
    def _first_attachment_url(self, kind: str) -> str:
        """Return the `url` field of the first attachment of `kind`,
        or '' if the bubble has no such attachment. Used by the
        save-photo / save-video / save-voice menu actions to pick
        the attachment to download."""
        for a in (self.msg.get("attachments") or []):
            if a.get("type") == kind:
                return a.get("url") or ""
        return ""

    @staticmethod
    def _ext_from_url(url: str, fallback: str) -> str:
        """Best-effort file extension from the path component of `url`.
        Returns `fallback` (with leading dot, e.g. '.m4a') when the URL
        has no usable suffix or one that looks implausible after
        stripping the query string."""
        try:
            path = url.split("?", 1)[0]
            dot   = path.rfind(".")
            slash = path.rfind("/")
            if dot > slash >= 0 and dot < len(path) - 1:
                ext = path[dot:]
                if len(ext) <= 6 and ext[1:].isalnum():
                    return ext
        except Exception:
            pass
        return fallback

    def _action_save_photo(self, *_):
        url = self._first_attachment_url("image")
        if not url:
            return
        ext = self._ext_from_url(url, ".jpg")
        self._save_url_via_dialog(
            url, f"groupme-photo{ext}", "Save Photo",
            authed=False, downloading_msg="Downloading photo…")

    def _action_save_video(self, *_):
        url = self._first_attachment_url("video")
        if not url:
            return
        ext = self._ext_from_url(url, ".mp4")
        self._save_url_via_dialog(
            url, f"groupme-video{ext}", "Save Video",
            authed=False, downloading_msg="Downloading video…")

    def _action_add_to_album(self, *_):
        """Open the album picker pre-loaded with this message's image
        and video attachments. Group-only — the menu predicate already
        gates on this, but a defensive recheck here keeps the handler
        safe if someone activates the action via keyboard / D-Bus."""
        atts = self.msg.get("attachments") or []
        media: list = []
        for a in atts:
            kind = a.get("type")
            if kind not in ("image", "video"):
                continue
            url = a.get("url") or ""
            if not url:
                continue
            media.append({
                "media_url":    url,
                "media_type":   kind,
                "media_source": "album",
            })
        if not media:
            return

        cv = getattr(self.win, "_chat_view", None)
        if cv is None or getattr(cv, "_is_dm", False):
            return
        group = getattr(cv, "_group", None)
        if not isinstance(group, dict):
            return

        # Late import — gallery.py pulls in widgets and would
        # circle back if imported at module-load time.
        from ..dialogs.gallery import AlbumPickerDialog
        AlbumPickerDialog(
            self.api, group, media, self.win).present(self.win)

    def _action_save_file(self, *_):
        """Save a `type:"file"` attachment — anything uploaded via the
        file picker (docs, video files, archives). Unlike photo/video
        which carry a public URL on the attachment, files require
        `api.download_file(cid, file_id, dest)` and the original
        filename comes from a separate `get_file_data` lookup."""
        fid = ""
        for a in (self.msg.get("attachments") or []):
            if a.get("type") == "file":
                fid = a.get("file_id") or ""
                break
        if not fid:
            return
        cid = self._conversation_id()
        api = self.api
        win = self.win
        if win is None:
            return
        try: win.toast("Preparing download…")
        except Exception: pass

        def fetch_meta():
            data = api.get_file_data(cid, [fid])
            meta = data.get(fid) or {}
            GLib.idle_add(self._show_save_file_dialog, cid, fid, meta)

        run_in_background(fetch_meta)

    def _show_save_file_dialog(self, cid: str, fid: str, meta: dict):
        win  = self.win
        api  = self.api
        name = meta.get("file_name") or f"groupme-file-{fid}"
        fd = Gtk.FileDialog()
        fd.set_title("Save Attachment")
        fd.set_initial_name(name)

        def on_chosen(fd, result):
            try:
                f    = fd.save_finish(result)
                dest = f.get_path()
            except GLib.Error:
                return   # user cancelled
            try: win.toast(f"Downloading {name}…")
            except Exception: pass

            def worker():
                ok = api.download_file(cid, fid, dest)
                def report():
                    try:
                        win.toast(
                            f"Saved to {dest}" if ok else "Save failed")
                    except Exception:
                        pass
                GLib.idle_add(report)

            run_in_background(worker)

        fd.save(win, None, on_chosen)

    def _action_save_voice(self, *_):
        url = self._first_attachment_url("audio")
        if not url:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Reuse the cached copy from playback if it's already on disk
        # — saves a round-trip and works offline.
        cached = ""
        try:
            p = CACHE_DIR / f"{_cache_key(url)}.audio"
            if p.exists():
                cached = str(p)
        except Exception:
            cached = ""
        self._save_url_via_dialog(
            url, f"groupme-voice-{stamp}.m4a", "Save Voice Message",
            authed=True, downloading_msg="Downloading voice message…",
            cached_path=cached)

    def _save_url_via_dialog(self, url: str, default_name: str,
                              title: str, authed: bool,
                              downloading_msg: str,
                              cached_path: str = ""):
        """Show a save-file dialog and stream `url` to the chosen path
        in a worker thread.

        `authed=True` routes through `api.download_audio` which sends
        the m.groupme.com Cookie token. Photo and video URLs are
        public so they go through plain `urlretrieve`. `cached_path`,
        when present, short-circuits the download with a local copy."""
        win = self.win
        if win is None or not url:
            return
        fd = Gtk.FileDialog()
        fd.set_title(title)
        fd.set_initial_name(default_name)

        api = self.api
        def on_chosen(fd, result):
            try:
                f    = fd.save_finish(result)
                dest = f.get_path()
            except GLib.Error:
                return   # user cancelled
            try: win.toast(downloading_msg)
            except Exception: pass

            def worker():
                ok = False
                if cached_path:
                    try:
                        import shutil
                        shutil.copy(cached_path, dest)
                        ok = True
                    except Exception:
                        ok = False
                if not ok:
                    if authed:
                        ok = api.download_audio(url, dest)
                    else:
                        try:
                            urllib.request.urlretrieve(url, dest)
                            ok = True
                        except Exception:
                            ok = False
                def report():
                    try:
                        win.toast(
                            f"Saved to {dest}" if ok else "Save failed")
                    except Exception:
                        pass
                GLib.idle_add(report)

            run_in_background(worker)

        fd.save(win, None, on_chosen)

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


class EditMessageDialog(Adw.Dialog):
    """Inline editor for a sent message. PUTs the new text on save and
    invokes the bubble's refresh() with the server response."""

    def __init__(self, bubble: MessageBubble):
        super().__init__()
        self._bubble = bubble
        self._in_flight = False
        self.set_title("Edit message")
        self.set_content_width(420)

        tv = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        hdr.pack_start(cancel_btn)
        self._save_btn = Gtk.Button(label="Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._save)
        hdr.pack_end(self._save_btn)
        tv.add_top_bar(hdr)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)

        self._tv = Gtk.TextView()
        self._tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._tv.set_pixels_above_lines(4)
        self._tv.set_pixels_below_lines(4)
        self._tv.get_buffer().set_text(bubble.msg.get("text") or "")

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self._tv)
        scroll.set_min_content_height(120)
        scroll.set_max_content_height(360)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        body.append(scroll)
        tv.set_content(body)
        self.set_child(tv)

    def _save(self, *_):
        if self._in_flight:
            return
        buf  = self._tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(),
                              buf.get_end_iter(), False).strip()
        if not text:
            return
        bubble  = self._bubble
        conv_id = bubble._conversation_id()
        mid     = str(bubble.msg.get("id"))

        # Lock the button + dialog while the request is in flight so
        # repeated clicks don't fan out into a wall of duplicate POSTs.
        self._in_flight = True
        self._save_btn.set_sensitive(False)
        self._save_btn.set_label("Saving…")

        def worker():
            return bubble.api.edit_message(conv_id, mid, text)

        def on_done(updated):
            self._in_flight = False
            if updated:
                # Stamp updated_at so the (edited) indicator shows
                # immediately — server response may be sparse.
                edited_msg = dict(bubble.msg)
                edited_msg["text"]       = text
                edited_msg["updated_at"] = int(
                    datetime.now().timestamp())
                if isinstance(updated, dict):
                    edited_msg.update({k: v for k, v in updated.items()
                                       if v is not None})
                bubble.update_text_from(edited_msg)
                try:
                    bubble.win.toast("Message updated")
                except Exception:
                    pass
                self.close()
            else:
                self._save_btn.set_label("Save")
                self._save_btn.set_sensitive(True)
                try:
                    bubble.win.toast("Failed to edit message")
                except Exception:
                    pass

        run_in_background(worker, on_done)


# ─────────────────────────── Chat View ───────────────────────────

