"""Banter — application Preferences dialog.

Two toggles in a single Background-activity group:

  • Run in background — closing the window hides it instead of
    quitting; the active push + notification path keeps running on
    the hidden window.
  • Run at startup    — register a host autostart .desktop via the
    XDG Background portal so `banter --background` launches with the
    user session and notifications survive reboots.
"""

from gi.repository import Adw

from .. import background_portal
from ..config import Config


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, config: Config):
        super().__init__()
        self.set_title("Preferences")
        self._config = config

        page = Adw.PreferencesPage()
        self.add(page)

        grp = Adw.PreferencesGroup(title="Background activity")
        page.add(grp)

        self._bg_switch = Adw.SwitchRow(
            title="Run in background",
            subtitle=("Closing the window keeps Banter running so you "
                      "continue to get message notifications."))
        self._bg_switch.set_active(
            bool(config.get_pref("close_to_background", False)))
        self._bg_switch.connect("notify::active", self._on_bg_toggle)
        grp.add(self._bg_switch)

        self._start_switch = Adw.SwitchRow(
            title="Run at startup",
            subtitle=("Start Banter automatically when you log in. "
                      "Required for background notifications across reboots."))
        self._start_switch.set_active(
            bool(config.get_pref("autostart_enabled", False)))
        self._start_switch.connect("notify::active",
                                    self._on_autostart_toggle)
        grp.add(self._start_switch)

    def _on_bg_toggle(self, sw, _pspec):
        self._config.set_pref("close_to_background", sw.get_active())

    def _on_autostart_toggle(self, sw, _pspec):
        enabled = sw.get_active()
        # Persist intent immediately. If the portal denies the request
        # below we roll the pref + switch back; until then the UI
        # reflects what the user just asked for.
        self._config.set_pref("autostart_enabled", enabled)

        def on_response(code: int):
            if code == 0:
                return
            # Cancelled or portal failure — revert silently. Block our
            # own handler while flipping the switch so we don't issue
            # a second portal request from the rollback.
            sw.handler_block_by_func(self._on_autostart_toggle)
            sw.set_active(not enabled)
            sw.handler_unblock_by_func(self._on_autostart_toggle)
            self._config.set_pref("autostart_enabled", not enabled)

        background_portal.request_background(
            autostart=enabled, on_response=on_response)
