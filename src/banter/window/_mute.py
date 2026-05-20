"""MuteMixin — bell-button menu + config-backed mute store.

Mixed into BanterWindow. Tracks per-conversation mute state via
`self._config.is_muted(key)`; the bell button on the per-chat header
reflects the current state and the menu offers timed presets +
permanent / unmute.
"""

import time

from gi.repository import Gio, GLib


class MuteMixin:
    # Timed-mute presets surfaced in the bell-button menu. Tuples of
    # (label, seconds) — seconds=-1 is "until I turn it back on" (the
    # config layer's permanent sentinel).
    _MUTE_PRESETS = (
        ("1 hour",                   3600),
        ("8 hours",                  8 * 3600),
        ("Until I turn it back on", -1),
    )

    @staticmethod
    def _mute_key(conv_type: str, conv_id) -> str:
        """Config key for the mute store. Groups use their bare gid
        (matches the legacy v1 schema); DMs are prefixed `dm:` so the
        key spaces don't collide."""
        s = str(conv_id)
        return f"dm:{s}" if conv_type == "dm" else s

    def is_conv_muted(self, conv_type: str, conv_id) -> bool:
        return self._config.is_muted(self._mute_key(conv_type, conv_id))

    def _refresh_mute_button(self, btn, conv_type: str, conv_id):
        muted = self.is_conv_muted(conv_type, conv_id)
        # Adwaita only ships notifications-DISABLED-symbolic, so we
        # bundle our own bell-without-slash for the unmuted state
        # (see data/meson.build).
        btn.set_icon_name(
            "notifications-disabled-symbolic" if muted
            else "banter-notifications-active-symbolic")
        btn.set_tooltip_text(
            "Unmute notifications" if muted else "Mute notifications")

    @staticmethod
    def _mute_menu_item(label: str, secs: int) -> Gio.MenuItem:
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(
            "win.set-mute", GLib.Variant.new_int32(secs))
        return item

    def _build_mute_menu(self, conv_type: str, conv_id) -> Gio.Menu:
        """Build the bell-button menu. 'Unmute' appears in its own
        section above the durations only when the conv is currently
        muted, so users always see a meaningful first option."""
        menu = Gio.Menu()
        if self.is_conv_muted(conv_type, conv_id):
            sec = Gio.Menu()
            sec.append_item(self._mute_menu_item("Unmute", 0))
            menu.append_section(None, sec)
        durations = Gio.Menu()
        for label, secs in self._MUTE_PRESETS:
            durations.append_item(self._mute_menu_item(label, secs))
        menu.append_section(None, durations)
        return menu

    def _apply_mute(self, conv_type: str, conv_id, secs: int):
        """Apply a mute change picked from the bell menu. Updates
        config, the bell icon, the sidebar row, and the menu model
        (so 'Unmute' appears/disappears on the next open)."""
        key = self._mute_key(conv_type, conv_id)
        if secs == 0:
            self._config.clear_mute(key)
            msg = "Notifications on"
        elif secs == -1:
            self._config.set_mute(key, -1)
            msg = "Muted until you turn it back on"
        else:
            until = int(time.time()) + secs
            self._config.set_mute(key, until)
            msg = f"Muted for {next(
                (lbl for lbl, s in self._MUTE_PRESETS if s == secs),
                f'{secs} seconds')}"

        if self._mute_btn is not None:
            self._refresh_mute_button(self._mute_btn, conv_type, conv_id)
            self._mute_btn.set_menu_model(
                self._build_mute_menu(conv_type, conv_id))
        row = self._rows.get(self._conv_key(conv_type, conv_id))
        if row is not None and hasattr(row, "set_muted"):
            row.set_muted(self._config.is_muted(key))
        try:
            self.toast(msg)
        except Exception:
            pass
