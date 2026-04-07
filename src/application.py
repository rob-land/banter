"""Banter — Adw.Application subclass and entry point."""

import sys
import urllib.parse
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, Gdk, Gio, GLib

from .config import Config
from .constants import APP_ID, APP_NAME, APP_VERSION, DEBUG, CONFIG_DIR, CACHE_DIR, dbg, log
from .css import APP_CSS
from .window import MainWindow


class BanterApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.connect("activate", self._on_activate)
        self.connect("open",     self._on_open)
        self.connect("startup",  self._on_startup)
        self._window = None

    def _on_startup(self, *_):
        """Register the app on the session D-Bus (required for notifications)
        and wire up the 'activate' action used by notification default-action."""
        activate_action = Gio.SimpleAction.new("activate", None)
        activate_action.connect("activate", lambda *_: self._bring_to_front())
        self.add_action(activate_action)

    def _bring_to_front(self):
        if self._window:
            self._window.present()

    def _on_activate(self, *_):
        self._load_css()
        if not self._window:
            self._window = MainWindow(self)
        self._window.present()

    def _on_open(self, app, files, n_files, hint):
        """Handle banter:// URI scheme redirects from the OAuth browser flow."""
        self._on_activate()   # ensure window exists
        for f in files:
            uri = f.get_uri()
            dbg("open-uri: %s", uri)
            if uri and "access_token=" in uri:
                parsed = urllib.parse.urlparse(uri)
                params = urllib.parse.parse_qs(parsed.query)
                if not params.get("access_token"):
                    params.update(urllib.parse.parse_qs(parsed.fragment))
                token = (params.get("access_token") or [""])[0].strip()
                if token and self._window:
                    # Deliver the token to whoever is waiting for it
                    GLib.idle_add(self._window.deliver_oauth_token, token)
                    return

    def _load_css(self):
        css = Gtk.CssProvider()
        css.load_from_string(APP_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


# ─────────────────────────── Entry Point ─────────────────────────

def main():
    if DEBUG:
        log.debug("=" * 60)
        log.debug("Banter  v%s  — DEBUG MODE", APP_VERSION)
        log.debug("Config : %s", CONFIG_DIR)
        log.debug("Cache  : %s", CACHE_DIR)
        log.debug("Python : %s", sys.version.split()[0])
        log.debug("=" * 60)
    elif "--help" in sys.argv or "-h" in sys.argv:
        print(f"Banter  v{APP_VERSION}")
        print("Usage: banter [OPTIONS]")
        print("  -d, --debug    Verbose debug logging")
        print("  -h, --help     Show this message")
        sys.exit(0)

    app = BanterApplication()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
