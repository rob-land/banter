"""Banter — ConversationRow and related sidebar row widgets."""

from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw

from ..constants import esc
from ..config import Config
from ..helpers import set_avatar_from_url


# ─────────────────────────── Group Row ───────────────────────────

def _fmt_conv_time(ts) -> str:
    """Format a message timestamp for the conversation list (like the GroupMe app)."""
    if not ts:
        return ""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    dt    = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    d     = dt.date()
    if d == today:
        return dt.strftime("%-I:%M %p")
    if d == today.__class__.fromordinal(today.toordinal() - 1):
        return "Yesterday"
    if d.year == today.year:
        return dt.strftime("%b %-d")
    return dt.strftime("%b %-d, %Y")


class ConversationRow(Adw.ActionRow):
    """Unified sidebar row for both group chats and direct messages.

    Matches the GroupMe app layout:
      [avatar]  Name                    [time]
                Last message preview    [🔕] [●]
    """
    def __init__(self, conv: dict, conv_type: str, config=None, me_id=None):
        """
        conv_type: "group" or "dm"
        config: Config instance for mute check (optional)
        me_id: current user's id — used to render "You: " in DM previews
        """
        super().__init__()
        self.conv      = conv
        self.conv_type = conv_type
        self._config   = config
        self._me_id    = str(me_id) if me_id is not None else None
        self.set_activatable(True)
        self.add_css_class("group-list-row")

        # ── Avatar ──
        if conv_type == "dm":
            other = conv.get("other_user", {})
            name  = other.get("name", "Direct Message")
            av_url = other.get("avatar_url", "")
        else:
            name   = conv.get("name", "Group")
            av_url = conv.get("image_url", "")

        av = Adw.Avatar(size=44, text=esc(name), show_initials=True)
        set_avatar_from_url(av, av_url)
        self.add_prefix(av)

        # ── Title ──
        self.set_title(esc(name))

        # ── Subtitle: "Sender: last message preview" ──
        sender, text = self._initial_preview()
        if text or sender:
            self.update_preview(sender, text)

        # ── Right-side meta box: [time / mute / dot] stacked ──
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_valign(Gtk.Align.CENTER)

        # Time label
        if conv_type == "dm":
            ts = (conv.get("last_message", {}).get("created_at") or
                  conv.get("updated_at"))
        else:
            ts = conv.get("messages", {}).get("last_message_created_at")
        self._time_lbl = Gtk.Label(label=_fmt_conv_time(ts))
        self._time_lbl.add_css_class("conv-time")
        self._time_lbl.set_halign(Gtk.Align.END)
        meta.append(self._time_lbl)

        # Bottom row: mute icon + unread dot
        bottom = Gtk.Box(spacing=4)
        bottom.set_halign(Gtk.Align.END)

        self._mute_icon = Gtk.Image.new_from_icon_name(
            "notifications-disabled-symbolic")
        self._mute_icon.add_css_class("muted-icon")
        self._mute_icon.set_pixel_size(12)
        self._mute_icon.set_visible(False)
        bottom.append(self._mute_icon)

        # Unread indicator — plain dot for 1, count pill for >1
        self._unread_dot   = Gtk.Box()
        self._unread_dot.add_css_class("unread-dot")
        self._unread_dot.set_size_request(8, 8)
        self._unread_dot.set_valign(Gtk.Align.CENTER)
        self._unread_dot.set_visible(False)

        self._unread_count = Gtk.Label()
        self._unread_count.add_css_class("unread-count")
        self._unread_count.set_valign(Gtk.Align.CENTER)
        self._unread_count.set_visible(False)

        bottom.append(self._unread_dot)
        bottom.append(self._unread_count)
        meta.append(bottom)

        self.add_suffix(meta)

        # Set initial state
        if conv_type == "group":
            unread = int(conv.get("messages", {}).get("unread_count") or 0)
            gid    = str(conv.get("id", ""))
        else:
            unread = int(conv.get("unread_count") or 0)
            gid    = None

        self.set_unread(unread)
        self._refresh_mute(gid)

    def _refresh_mute(self, gid):
        if self._config and gid:
            self._mute_icon.set_visible(self._config.is_muted(gid))

    def set_unread(self, count: int):
        count = int(count or 0)
        # Stash the numeric count so `bump_unread()` / callers can read
        # it without parsing widget text back into an int.
        self._unread_count_n = count
        if count <= 0:
            self._unread_dot.set_visible(False)
            self._unread_count.set_visible(False)
        elif count == 1:
            self._unread_dot.set_visible(True)
            self._unread_count.set_visible(False)
        else:
            self._unread_dot.set_visible(False)
            self._unread_count.set_text(str(count) if count < 100 else "99+")
            self._unread_count.set_visible(True)

    def bump_unread(self):
        """Increment the unread count by 1. Safer than reading the
        badge text back with int()."""
        self.set_unread(getattr(self, "_unread_count_n", 0) + 1)

    def update_time(self, ts):
        self._time_lbl.set_text(_fmt_conv_time(ts))

    # ── Preview helpers ──────────────────────────────────────────────
    PREVIEW_MAX = 80

    def _initial_preview(self) -> tuple:
        """Derive (sender, text) for the subtitle from the stored conv dict."""
        conv = self.conv
        if self.conv_type == "group":
            preview = conv.get("messages", {}).get("preview", {}) or {}
            sender  = preview.get("nickname", "")
            text    = (preview.get("text") or "").strip()
            if not text and preview.get("attachments"):
                text = "📎 attachment"
            return sender, text

        # DM
        lm        = conv.get("last_message", {}) or {}
        text      = (lm.get("text") or "").strip()
        sender_id = str(lm.get("sender_id") or lm.get("user_id") or "")
        if self._me_id and sender_id == self._me_id:
            sender = "You"
        else:
            sender = (conv.get("other_user", {}) or {}).get("name", "")
        if not text and lm.get("attachments"):
            text = "📎 attachment"
        return sender, text

    def update_preview(self, sender: str, text: str):
        """Refresh the subtitle to 'Sender: text' (truncated if too long)."""
        text = (text or "").strip()
        if not text:
            text = "📎 attachment"
        combined = f"{sender}: {text}" if sender else text
        if len(combined) > self.PREVIEW_MAX:
            combined = combined[: self.PREVIEW_MAX - 1].rstrip() + "…"
        self.set_subtitle(esc(combined))


class ContactRow(Adw.ActionRow):
    def __init__(self, contact: dict):
        super().__init__()
        self.contact = contact
        name = contact.get("name") or contact.get("nickname", "Unknown")
        self.set_title(esc(name))

        parts = []
        if contact.get("phone_number"):
            parts.append(contact["phone_number"])
        if contact.get("email"):
            parts.append(contact["email"])
        if parts:
            self.set_subtitle(esc("  ·  ".join(parts)))

        self.set_activatable(True)

        av = Adw.Avatar(size=40, text=esc(name), show_initials=True)
        url = contact.get("avatar_url") or contact.get("image_url", "")
        set_avatar_from_url(av, url)
        self.add_prefix(av)

        self.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))


# ─────────────────────────── Dialogs ─────────────────────────────
