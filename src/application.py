"""Banter — Adw.Application subclass and entry point."""

import sys
import urllib.parse
from pathlib import Path
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
        self._register_bundled_icons()
        if not self._window:
            self._window = MainWindow(self)
        self._window.present()

    def _register_bundled_icons(self):
        """Add our bundled icon directories to the GTK icon theme.

        When running from the meson staging root (the dev workflow,
        `PYTHONPATH=…/site-packages …/bin/banter`) GTK only searches
        XDG-default theme paths and won't find icons we ship under
        `<prefix>/share/icons`. Walk up from this module hunting for
        a `share/icons` sibling — the directory layout differs
        between Fedora's (`lib/python<X.Y>/site-packages/banter/`)
        and the historical pkgdatadir layout (`share/banter/`), so
        a probe is more robust than a fixed parents[N]."""
        try:
            display = Gdk.Display.get_default()
            if display is None:
                return
            theme = Gtk.IconTheme.get_for_display(display)

            here = Path(__file__).resolve()
            # Probe up to 8 levels — handles all layouts we ship.
            for ancestor in here.parents[:8]:
                candidate = ancestor / "share" / "icons"
                if candidate.is_dir():
                    theme.add_search_path(str(candidate))
                    dbg("registered icon search path: %s", candidate)
                    return
            dbg("no bundled icon path found relative to %s", here)
        except Exception as e:
            dbg("icon search-path registration failed: %s", e)

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
