"""Banter — Adw.Application subclass and entry point."""

import logging
import sys
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .async_utils import BackgroundRunner
from .config import Config
from .constants import APP_ID, APP_NAME, APP_VERSION, BACKGROUND, CACHE_DIR, CONFIG_DIR
from .logging_setup import configure_logging
from .notifier import BanterNotifier
from .window import BanterWindow

log = logging.getLogger(__name__)


class BanterApplication(Adw.Application):
    def __init__(self, *, background: bool = False):
        super().__init__(application_id=APP_ID)
        self._background = background
        self._window     = None
        self._notifier   = None
        self.runner: BackgroundRunner | None = None

    def do_startup(self):
        """Register the app on the session D-Bus (required for notifications)
        and wire up the 'activate' action used by notification default-action."""
        Adw.Application.do_startup(self)

        # Single Config owned by the app — every component that needs
        # accounts / prefs / mutes pulls this same instance. Config
        # has no on-disk reload path, so two instances diverge the
        # moment one writes; the previous "every component calls
        # Config()" pattern bit us on the Preferences dialog and
        # would have re-bit us anywhere notifier + window ever ran
        # concurrently.
        self.config = Config()

        self.runner = BackgroundRunner(name="banter-bg")

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

        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", self._show_preferences)
        self.add_action(prefs_action)
        self.set_accels_for_action("app.preferences", ["<ctrl>comma"])

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
        # Notifications activated from --background mode hit this path
        # before any window exists; route through do_activate so the
        # window gets built on demand (and the headless notifier shut
        # down) the same way an explicit launch would.
        self.do_activate()

    def do_shutdown(self):
        if self.runner is not None:
            self.runner.stop()
            self.runner = None
        Adw.Application.do_shutdown(self)

    def _on_call_join(self, _action, param):
        """Handler for the `app.call-join` notification button. Re-uses
        the window's existing call-launch path so we share the toast
        and error handling. In --background mode the window may not
        exist yet — _bring_to_front() will build it before we route."""
        if param is None:
            return
        gid = param.get_string()
        if not gid:
            return
        self._bring_to_front()
        if self._window:
            self._window._on_call_clicked(None, gid)

    def do_activate(self):
        # Headless launch path: spin up the notifier, hold the app
        # alive without a visible window, and exit early. Notification
        # clicks (or a second `banter` invocation) will re-enter this
        # method and fall through to the window-building branch below.
        if self._background and self._window is None and self._notifier is None:
            self._notifier = BanterNotifier(self)
            if self._notifier.start():
                self.hold()
                return
            # No active account → nothing for the daemon to do.
            self._notifier = None
            return

        self._load_css()
        self._register_bundled_icons()
        if not self._window:
            self._window = BanterWindow(self)
            # If a notifier was holding the app alive, retire it now —
            # the window owns the API + push from this point on. The
            # window's own _start_push reconnects the WebSocket; we
            # accept the brief overlap rather than transplanting the
            # live socket (a future refactor).
            if self._notifier is not None:
                self._notifier.stop()
                self._notifier = None
                self.release()
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
                    log.debug("registered icon search path: %s", candidate)
                    return
            log.debug("no bundled icon path found relative to %s", here)
        except Exception as e:
            log.debug("icon search-path registration failed: %s", e)

    def _load_css(self):
        css = Gtk.CssProvider()
        css.load_from_resource('/land/rob/banter/ui/style.css')
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
        builder = Gtk.Builder.new_from_resource('/land/rob/banter/ui/help-overlay.ui')
        win = builder.get_object("help_overlay")
        if self._window:
            win.set_transient_for(self._window)
        win.present()

    def _show_preferences(self, *_):
        from .dialogs.preferences import PreferencesDialog
        dlg = PreferencesDialog(self.config)
        dlg.present(self._window)


# ─────────────────────────── Entry Point ─────────────────────────

def main():
    configure_logging()

    if "--help" in sys.argv or "-h" in sys.argv:
        print(f"Banter  v{APP_VERSION}")
        print("Usage: banter [OPTIONS]")
        print("  -d, --debug    Verbose debug logging")
        print("  --background   Run as a headless notification daemon")
        print("                 (no window; intended for autostart at login)")
        print("  -h, --help     Show this message")
        sys.exit(0)

    log.debug("=" * 60)
    log.debug("Banter  v%s", APP_VERSION)
    log.debug("Config : %s", CONFIG_DIR)
    log.debug("Cache  : %s", CACHE_DIR)
    log.debug("Python : %s", sys.version.split()[0])
    log.debug("=" * 60)

    app = BanterApplication(background=BACKGROUND)
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
