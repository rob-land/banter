"""Banter — LoginDialog (OAuth flow)."""

from datetime import datetime
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf

from ..constants import dbg, esc, CACHE_DIR
from ..async_utils import run_in_background
from ..api import GroupMeAPI
from ..helpers import set_avatar_from_url, load_image_async, _cache_key

class LoginDialog(Adw.Dialog):
    """
    GroupMe sign-in via OAuth 2.0 implicit flow.

    Flow:
      1. User enters their Client ID (from dev.groupme.com/applications).
      2. App opens the system browser to GroupMe's authorisation page.
      3. A local HTTP server on port 7654 captures the redirect with the token.
      4. Token is validated via /users/me and the session is established.

    The user must register their application at dev.groupme.com with the
    callback URL set to:  http://localhost:7654
    """

    def __init__(self, parent, on_login):
        super().__init__()
        self._parent   = parent
        self._on_login = on_login
        self._api      = GroupMeAPI()
        self._server   = None

        self.set_title("Sign In to GroupMe")
        self.set_content_width(440)
        self.set_content_height(520)

        tv  = Adw.ToolbarView()
        hdr = Adw.HeaderBar()
        hdr.set_show_back_button(False)
        tv.add_top_bar(hdr)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_valign(Gtk.Align.CENTER)
        outer.set_halign(Gtk.Align.FILL)
        outer.set_spacing(18)
        outer.set_margin_start(28); outer.set_margin_end(28)

        # ── Logo ──
        logo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        logo_box.set_halign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("chat-message-new-symbolic")
        icon.set_pixel_size(56)
        icon.add_css_class("accent")
        logo_box.append(icon)
        lbl_title = Gtk.Label(label="GroupMe")
        lbl_title.add_css_class("title-1")
        logo_box.append(lbl_title)
        sub = Gtk.Label(label="Sign in with your GroupMe account")
        sub.add_css_class("dim-label")
        logo_box.append(sub)
        outer.append(logo_box)

        # ── Client ID entry ──
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("login-card")
        form = Adw.PreferencesGroup()
        self._client_id_row = Adw.EntryRow(title="Client ID")
        self._client_id_row.set_tooltip_text(
            "Your application's Client ID from dev.groupme.com/applications")
        form.add(self._client_id_row)
        card.append(form)
        outer.append(card)

        # ── Setup hint ──
        hint = Gtk.Label()
        hint.set_markup(
            '<span size="small">'
            'Need a Client ID?  '
            '<a href="https://dev.groupme.com/applications/new">'
            'Create an application</a> at dev.groupme.com.\n'
            'Set the <b>Callback URL</b> to: '
            '<tt>http://localhost:7654</tt>'
            '</span>'
        )
        hint.set_use_markup(True)
        hint.set_xalign(0)
        hint.set_wrap(True)
        hint.add_css_class("dim-label")
        outer.append(hint)

        # ── Status / error label ──
        self._status = Gtk.Label()
        self._status.add_css_class("dim-label")
        self._status.set_visible(False)
        self._status.set_wrap(True)
        outer.append(self._status)

        self._err = Gtk.Label()
        self._err.add_css_class("error-label")
        self._err.set_visible(False)
        self._err.set_wrap(True)
        outer.append(self._err)

        # ── Sign-in button ──
        self._btn = Gtk.Button(label="Sign In with Browser")
        self._btn.add_css_class("suggested-action")
        self._btn.add_css_class("pill")
        self._btn.connect("clicked", self._start_oauth)
        outer.append(self._btn)

        # ── Sign-up link ──
        link_lbl = Gtk.Label()
        link_lbl.set_markup(
            '<span size="small">'
            'Don\'t have an account?  '
            '<a href="https://groupme.com/signup">Sign up on groupme.com</a>'
            '</span>'
        )
        link_lbl.set_use_markup(True)
        outer.append(link_lbl)

        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self._on_key)
        self._client_id_row.add_controller(kc)

        tv.set_content(outer)
        self.set_child(tv)
        dbg("LoginDialog: opened (OAuth flow)")

    def _on_key(self, ctrl, keyval, *_):
        if keyval == Gdk.KEY_Return:
            self._start_oauth()
            return True
        return False

    def _start_oauth(self, *_):
        client_id = self._client_id_row.get_text().strip()
        if not client_id:
            self._show_err("Please enter your Client ID from dev.groupme.com")
            return

        self._btn.set_sensitive(False)
        self._btn.set_label("Waiting for browser…")
        self._err.set_visible(False)
        self._show_status("Opening browser — please log in and authorise the app…")

        # Start the callback server first
        self._server = OAuthCallbackServer(
            on_token=self._on_token_received,
            on_error=self._on_oauth_error,
        )
        self._server.start()

        # Open the system browser to the GroupMe authorise URL
        callback = f"http://localhost:{OAUTH_PORT}"
        auth_url = (f"{OAUTH_AUTHORIZE_URL}"
                    f"?client_id={urllib.parse.quote(client_id)}"
                    f"&redirect_uri={urllib.parse.quote(callback)}")
        dbg("LoginDialog: opening browser → %s", auth_url)
        try:
            Gio.AppInfo.launch_default_for_uri(auth_url, None)
        except Exception as e:
            self._on_oauth_error(f"Could not open browser: {e}")

    def _on_token_received(self, token: str):
        """Called on the GLib main thread after the local server captures the token."""
        dbg("LoginDialog: token received, verifying…")
        self._show_status("Token received — verifying…")
        if self._server:
            self._server.stop()
            self._server = None

        def worker():
            ok, result = self._api.verify_token(token)
            GLib.idle_add(self._on_verify_done, ok, result)

        run_in_background(worker)

    def _on_verify_done(self, ok, result):
        self._btn.set_sensitive(True)
        self._btn.set_label("Sign In with Browser")
        self._status.set_visible(False)
        if ok:
            dbg("LoginDialog: success – user=%s", result.get("name"))
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

    def _show_status(self, msg: str):
        self._status.set_text(msg)
        self._status.set_visible(True)

    def _show_err(self, msg: str):
        self._err.set_text(msg)
        self._err.set_visible(True)


# ─────────────────────────── Members Dialog ──────────────────────
