"""ContextMenuMixin — right-click + long-press gesture wiring and the
Gio.Menu / Gtk.PopoverMenu the bubble shows.

The menu is the single entry point to reply / copy / pin / save /
edit / delete / add-to-album. All action callbacks resolve to methods
on the host class via MRO.
"""

from datetime import datetime

from gi.repository import Gdk, Gio, Gtk


class ContextMenuMixin:
    # GroupMe enforces a server-side time window for editing one's own
    # messages. We hide the Edit menu past it so the user doesn't see
    # a confusing API failure on a stale message. The actual server
    # window is somewhere in the ~10-15 min range; we cap at 10 min
    # to stay conservatively inside it.
    EDIT_WINDOW_SECS = 600   # 10 minutes
    EDIT_ENABLED     = True

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
