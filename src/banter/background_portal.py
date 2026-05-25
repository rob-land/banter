"""Banter — autostart entry management.

The XDG Background portal would be the conventional channel to
register a per-user autostart .desktop, but Phosh (FuriOS,
postmarketOS, …) ships an xdg-desktop-portal with no Background
implementer — `RequestBackground` hangs forever waiting for a
Response signal that never gets emitted. Verified on FuriOS 14:
introspection on /org/freedesktop/portal/desktop shows no
`org.freedesktop.portal.Background` interface, and none of the
loaded backends (gtk, phosh, phosh-shell, phrosh, gnome-keyring)
declares it either.

So write the .desktop directly. The user already consented by
flipping the switch in Preferences, so the portal's permission
dialog is redundant; same end-state on GNOME desktop and Phosh.

Inside Flatpak we need write access to the host's autostart dir;
the manifest grants it via `--filesystem=xdg-config/autostart:create`.

Path used: ${XDG_CONFIG_HOME or ~/.config}/autostart/<APP_ID>.desktop

The function keeps its old (portal-era) `on_response(code)` callback
contract — 0 = success, 2 = failure — so the existing toggle UI
that rolls back on non-zero still works without changes.
"""

import logging
import os
from pathlib import Path

from gi.repository import GLib

from .constants import APP_ID, APP_NAME

log = logging.getLogger(__name__)


def autostart_commandline() -> list:
    """The argv we want the autostart entry to invoke. Inside Flatpak
    the host autostart .desktop must wrap the call in `flatpak run`,
    since the host's PATH has no `banter` binary."""
    if os.path.exists("/.flatpak-info"):
        return ["flatpak", "run", "--command=banter", APP_ID, "--background"]
    return ["banter", "--background"]


def _autostart_path() -> Path:
    if os.path.exists("/.flatpak-info"):
        # Inside the sandbox XDG_CONFIG_HOME is rewritten to the
        # per-app dir (~/.var/app/<appid>/config). The host's
        # ~/.config/autostart is bind-mounted at the literal
        # ~/.config/autostart path via the manifest's
        # --filesystem=xdg-config/autostart:create grant, so target
        # that path directly rather than the redirected env var.
        base = str(Path.home() / ".config")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / f"{APP_ID}.desktop"


def _desktop_file_body(commandline: list) -> str:
    exec_line = " ".join(GLib.shell_quote(arg) for arg in commandline)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={exec_line}\n"
        f"X-Flatpak={APP_ID}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Phase=Applications\n"
        "X-GNOME-Autostart-Delay=3\n"
        "NoDisplay=true\n"
    )


def request_background(*, autostart: bool, commandline: list = None,
                       on_response=None, **_ignored):
    """Write or remove the per-user autostart .desktop entry for
    `banter --background`. Invokes `on_response(0)` on success or
    `on_response(2)` on filesystem failure, scheduled on the GLib
    main loop so the UI handler runs in the right context.

    **_ignored swallows kwargs that the portal-era signature accepted
    (e.g. parent_xdg_handle) without forcing callers to change.
    """
    if commandline is None:
        commandline = autostart_commandline()

    path = _autostart_path()
    try:
        if autostart:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_desktop_file_body(commandline))
            log.info("autostart: wrote %s", path)
        else:
            try:
                path.unlink()
                log.info("autostart: removed %s", path)
            except FileNotFoundError:
                pass
        code = 0
    except OSError as e:
        log.warning("autostart: filesystem error: %s", e)
        code = 2

    if on_response:
        GLib.idle_add(on_response, code)
