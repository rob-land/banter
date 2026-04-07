"""Banter — OAuth 2.0 sign-in.

Uses a local HTTP server on port 7654 to capture the access token.
With --share=network in the Flatpak manifest the sandbox shares the host
network namespace, so the browser can reach localhost:7654 inside the app.

The banter:// URI scheme is also registered as a secondary handler so that
on mobile platforms where localhost loopback may not be available, the OS
can deliver the redirect directly to the running application via GApplication.

GroupMe app registration at dev.groupme.com should set Callback URL to:
  http://localhost:7654

(The banter://oauth/callback alternative works too if you prefer it and
update the registered callback URL accordingly.)
"""

import threading
import urllib.parse
import http.server

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio

from .constants import OAUTH_AUTHORIZE_URL, APP_NAME, APP_VERSION, dbg
from .api import GroupMeAPI

# ── Bundled OAuth credentials ─────────────────────────────────────────
# Register your app at dev.groupme.com/applications.
# Set Callback URL to: http://localhost:7654
BANTER_CLIENT_ID = "5JNvmRNJsERSOyE1ntQyziEJuV9VT9jWlo5OUfh0OB6mwNxw"
OAUTH_PORT       = 7654
OAUTH_CALLBACK   = f"http://localhost:{OAUTH_PORT}"


# ─────────────────────────── OAuth Callback Server ───────────────────

class OAuthCallbackServer:
    """Local HTTP server that captures the GroupMe OAuth redirect.

    Listens on http://localhost:7654.  With --share=network in the Flatpak
    manifest the sandbox shares the host network namespace so the browser
    (running outside the sandbox) can reach this server.
    """

    def __init__(self, on_token, on_error):
        self._on_token = on_token
        self._on_error = on_error
        self._server   = None

    def start(self):
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                token  = (params.get("access_token") or [""])[0].strip()

                if token:
                    body = (b"<html><body style='font-family:sans-serif;"
                            b"text-align:center;padding:60px'>"
                            b"<h2>Signed in to Banter!</h2>"
                            b"<p>You can close this tab and return to the app.</p>"
                            b"<script>window.close()</script></body></html>")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", len(body))
                    self.end_headers()
                    self.wfile.write(body)
                    GLib.idle_add(outer._on_token, token)
                else:
                    body = b"<html><body><p>No token received.</p></body></html>"
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", len(body))
                    self.end_headers()
                    self.wfile.write(body)
                    GLib.idle_add(outer._on_error, "No access token in redirect")
                threading.Thread(target=outer._server.shutdown, daemon=True).start()

            def log_message(self, fmt, *args):
                dbg("oauth-server: " + fmt, *args)

        try:
            self._server = http.server.HTTPServer(("127.0.0.1", OAUTH_PORT), _Handler)
        except OSError as e:
            GLib.idle_add(self._on_error,
                          f"Could not start local server on port {OAUTH_PORT}: {e}")
            return

        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        dbg("oauth-server: listening on port %d", OAUTH_PORT)

    def stop(self):
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()
            self._server = None


# ─────────────────────────── Login Dialog ────────────────────────────

class LoginDialog(Adw.Dialog):
    """One-click sign-in dialog.

    Opens the system browser to the GroupMe login page.  The access token is
    captured either via the local HTTP server (desktop/Flatpak with
    --share=network) or via the banter:// URI scheme (mobile fallback).
    """

    def __init__(self, parent, on_login):
        super().__init__()
        self._parent   = parent
        self._on_login = on_login
        self._api      = GroupMeAPI()
        self._server   = None

        self.set_title(f"Sign in to {APP_NAME}")
        self.set_content_width(400)
        self.set_content_height(-1)
        self.connect("closed", self._on_dialog_closed)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        hdr.set_show_back_button(False)
        tv.add_top_bar(hdr)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        outer.set_valign(Gtk.Align.CENTER)
        outer.set_margin_start(32); outer.set_margin_end(32)
        outer.set_margin_top(32);   outer.set_margin_bottom(32)

        icon = Gtk.Image.new_from_icon_name("chat-message-new-symbolic")
        icon.set_pixel_size(64)
        icon.add_css_class("accent")
        outer.append(icon)

        title_lbl = Gtk.Label(label=APP_NAME)
        title_lbl.add_css_class("title-1")
        outer.append(title_lbl)

        sub_lbl = Gtk.Label(label="Sign in with your GroupMe account")
        sub_lbl.add_css_class("dim-label")
        sub_lbl.set_wrap(True)
        sub_lbl.set_justify(Gtk.Justification.CENTER)
        outer.append(sub_lbl)

        self._status = Gtk.Label()
        self._status.add_css_class("dim-label")
        self._status.set_visible(False)
        self._status.set_wrap(True)
        self._status.set_justify(Gtk.Justification.CENTER)
        outer.append(self._status)

        self._err = Gtk.Label()
        self._err.add_css_class("error-label")
        self._err.set_visible(False)
        self._err.set_wrap(True)
        outer.append(self._err)

        self._btn = Gtk.Button(label="Sign In with Browser")
        self._btn.add_css_class("suggested-action")
        self._btn.add_css_class("pill")
        self._btn.connect("clicked", self._start_oauth)
        outer.append(self._btn)

        link_lbl = Gtk.Label()
        link_lbl.set_markup(
            '<span size="small">'
            "Don't have an account?  "
            '<a href="https://groupme.com/signup">Sign up on groupme.com</a>'
            "</span>"
        )
        link_lbl.set_use_markup(True)
        outer.append(link_lbl)

        tv.set_content(outer)
        self.set_child(tv)
        dbg("LoginDialog: opened (localhost OAuth)")

    # ── OAuth flow ──────────────────────────────────────────────────

    def _start_oauth(self, *_):
        self._btn.set_sensitive(False)
        self._btn.set_label("Waiting for browser…")
        self._err.set_visible(False)
        self._show_status(
            "Your browser has been opened — log in to GroupMe and authorise Banter.")

        self._server = OAuthCallbackServer(
            on_token=self._on_token_received,
            on_error=self._on_oauth_error,
        )
        self._server.start()

        auth_url = (f"{OAUTH_AUTHORIZE_URL}"
                    f"?client_id={BANTER_CLIENT_ID}"
                    f"&redirect_uri={urllib.parse.quote(OAUTH_CALLBACK)}")
        dbg("LoginDialog: opening browser → %s", auth_url)
        try:
            Gio.AppInfo.launch_default_for_uri(auth_url, None)
        except Exception as e:
            self._on_oauth_error(f"Could not open browser: {e}")

    def receive_token(self, token: str):
        """Called by MainWindow.deliver_oauth_token() when the OS delivers
        the banter:// URI scheme redirect (mobile fallback path)."""
        dbg("LoginDialog: token received via URI scheme")
        if self._server:
            self._server.stop()
            self._server = None
        self._on_token_received(token)

    def _on_token_received(self, token: str):
        dbg("LoginDialog: token received, verifying…")
        self._show_status("Token received — verifying…")
        if self._server:
            self._server.stop()
            self._server = None

        def worker():
            ok, result = self._api.verify_token(token)
            GLib.idle_add(self._on_verify_done, ok, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_verify_done(self, ok, result):
        self._btn.set_sensitive(True)
        self._btn.set_label("Sign In with Browser")
        self._status.set_visible(False)
        if ok:
            dbg("LoginDialog: success – user=%s", result.get("name"))
            try:
                self.disconnect_by_func(self._on_dialog_closed)
            except Exception:
                pass
            self.close()
            self._on_login(self._api.token, result)
        else:
            errs = result if isinstance(result, list) else [str(result)]
            self._show_err(", ".join(errs))

    def _on_oauth_error(self, msg: str):
        self._btn.set_sensitive(True)
        self._btn.set_label("Sign In with Browser")
        self._status.set_visible(False)
        self._show_err(msg)
        if self._server:
            self._server.stop()
            self._server = None

    def _on_dialog_closed(self, *_):
        dbg("LoginDialog: closed without login — quitting")
        if self._server:
            self._server.stop()
        app = self._parent.get_application() if self._parent else None
        if app:
            app.quit()

    # ── Helpers ──────────────────────────────────────────────────────

    def _show_status(self, msg: str):
        self._status.set_text(msg)
        self._status.set_visible(True)

    def _show_err(self, msg: str):
        self._err.set_text(msg)
        self._err.set_visible(True)
