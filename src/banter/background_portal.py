"""Banter — XDG Background portal helper.

Wraps `org.freedesktop.portal.Background.RequestBackground` so the
Notifications-prefs toggle can register/revoke an autostart entry for
`banter --background`. Works both inside Flatpak (xdg-desktop-portal
proxies to host) and outside (writes ~/.config/autostart/ directly).

Response codes from the portal:
  0 — granted (autostart file written or removed)
  1 — user cancelled the prompt
  2 — request ended in some other way (treat as failure)
"""

import os
import secrets as _stdlib_secrets

from gi.repository import GLib, Gio

from .constants import APP_ID, dbg


def autostart_commandline() -> list:
    """The argv we want the autostart entry to invoke. Inside Flatpak
    the host autostart .desktop must wrap the call in `flatpak run`,
    since the host's PATH has no `banter` binary."""
    if os.path.exists("/.flatpak-info"):
        return ["flatpak", "run", f"--command=banter", APP_ID, "--background"]
    return ["banter", "--background"]


def request_background(*, autostart: bool, commandline: list = None,
                       parent_xdg_handle: str = "",
                       on_response=None):
    """Call `RequestBackground` asynchronously; on completion invoke
    `on_response(code)` with the portal response code (0 = granted).

    Best-effort: if the session bus or the portal isn't reachable we
    invoke `on_response(2)` so callers can revert UI state cleanly
    rather than getting wedged.
    """
    if commandline is None:
        commandline = autostart_commandline()

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception as e:
        dbg("portal: no session bus (%s)", e)
        if on_response:
            GLib.idle_add(on_response, 2)
        return

    # Predict the Request object path so we can subscribe to its
    # Response signal *before* invoking the method — otherwise a fast
    # portal could fire the signal before our subscriber is wired up.
    handle_token = "banter_" + _stdlib_secrets.token_hex(8)
    sender = bus.get_unique_name() or ""
    sender_for_path = sender.lstrip(":").replace(".", "_")
    request_path = (
        f"/org/freedesktop/portal/desktop/request/"
        f"{sender_for_path}/{handle_token}")

    sub_id = [None]

    def _emit(code: int):
        if sub_id[0] is not None:
            try:
                bus.signal_unsubscribe(sub_id[0])
            except Exception:
                pass
            sub_id[0] = None
        if on_response:
            on_response(int(code))

    def _on_response(_conn, _sender, _path, _iface, _signal, params):
        try:
            code, _results = params.unpack()
        except Exception:
            code = 2
        _emit(code)

    sub_id[0] = bus.signal_subscribe(
        "org.freedesktop.portal.Desktop",
        "org.freedesktop.portal.Request",
        "Response",
        request_path,
        None,
        Gio.DBusSignalFlags.NONE,
        _on_response)

    options = GLib.Variant("a{sv}", {
        "handle_token":     GLib.Variant("s", handle_token),
        "reason":           GLib.Variant("s",
            "Deliver GroupMe message notifications when Banter isn't open."),
        "autostart":        GLib.Variant("b", bool(autostart)),
        "commandline":      GLib.Variant("as", list(commandline)),
        "dbus-activatable": GLib.Variant("b", False),
    })

    def _on_call_done(src, res, _user):
        try:
            src.call_finish(res)
        except Exception as e:
            dbg("portal: RequestBackground failed: %s", e)
            _emit(2)

    bus.call(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Background",
        "RequestBackground",
        GLib.Variant("(sa{sv})", (parent_xdg_handle, options)),
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        _on_call_done,
        None)
