"""Banter — Adw.Application subclass and entry point."""

import sys
import urllib.parse
from pathlib import Path

from gi.repository import Gtk, Adw, Gdk, Gio, GLib

from .config import Config
from .constants import APP_ID, APP_NAME, APP_VERSION, DEBUG, CONFIG_DIR, CACHE_DIR, dbg, log
from .window import BanterWindow


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

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.add_action(about_action)

        shortcuts_action = Gio.SimpleAction.new("shortcuts", None)
        shortcuts_action.connect("activate", self._show_shortcuts)
        self.add_action(shortcuts_action)
        self.set_accels_for_action("app.shortcuts", ["<ctrl>question"])

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<ctrl>q"])

        # Notification button: Join the call. The detailed action passes
        # the conversation id as the string parameter; we route to the
        # window's existing call-launch path (fetch a fresh meeting URL
        # via /v3/conversations/{cid}/call and open it in the browser).
        # The Faye `group.call.started` event's `meeting_id` lacks the
        # passcode, so re-fetching is required — can't just open the
        # event-supplied URL.
        call_join_action = Gio.SimpleAction.new(
            "call-join", GLib.VariantType.new("s"))
        call_join_action.connect("activate", self._on_call_join)
        self.add_action(call_join_action)

    def _bring_to_front(self):
        if self._window:
            self._window.present()

    def _on_call_join(self, _action, param):
        """Handler for the `app.call-join` notification button. Re-uses
        the window's existing call-launch path so we share the toast
        and error handling."""
        if not self._window or param is None:
            return
        gid = param.get_string()
        if not gid:
            return
        self._bring_to_front()
        self._window._on_call_clicked(None, gid)

    def _on_activate(self, *_):
        self._load_css()
        self._register_bundled_icons()
        if not self._window:
            self._window = BanterWindow(self)
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
        css.load_from_resource('/land/rob/Banter/ui/style.css')
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _show_about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=APP_VERSION,
            developer_name="Rob Daniel",
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2025 Rob Daniel",
            website="https://codeberg.org/robland/banter",
            issue_url="https://codeberg.org/robland/banter/issues",
        )
        about.add_acknowledgement_section(
            "Generated by",
            ("Claude (Anthropic)\nhttps://claude.com",),
        )
        about.present(self._window)

    def _show_shortcuts(self, *_):
        builder = Gtk.Builder.new_from_resource('/land/rob/Banter/ui/help-overlay.ui')
        win = builder.get_object("help_overlay")
        if self._window:
            win.set_transient_for(self._window)
        win.present()


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
