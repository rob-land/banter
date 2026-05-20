"""ActionsMixin — share group, group settings, members, calls, find,
mark-all-read.

Mixed into BanterWindow. The targets of the various view-more menu
items and the toolbar-level actions registered in `_setup_actions`.
"""

import logging

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..async_utils import run_in_background
from ..constants import esc
from ..dialogs.members import MembersDialog
from ..dialogs.settings import GroupSettingsDialog
from ..widgets.base import StandardDialog

log = logging.getLogger(__name__)


class ActionsMixin:
    # ── Call (Teams meeting) ──
    def _on_call_clicked(self, _btn, conv_id):
        """Fetch the call session for this conversation and open the
        Teams meeting URL in the system browser. The official client
        embeds Azure Communication Services' Teams composite; from a
        Linux app without that SDK, the browser is the right place
        for the user to grant camera/mic and join the call."""
        self.toast("Starting call…")
        api = self._api

        def worker():
            r = api.get_call(conv_id)
            GLib.idle_add(self._open_call, r)

        run_in_background(worker)

    def _open_call(self, call: dict):
        if not call:
            self.toast("Couldn't start call")
            return
        url = call.get("meeting_id", "")
        if not url:
            self.toast("Couldn't start call")
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception as e:
            log.debug("call launch failed: %s", e)
            self.toast("Couldn't open browser for call")

    # ── Share group ──
    def _share_group(self, group: dict):
        """Show share dialog with copy button, system share, and QR code."""
        share_url = group.get("share_url") or group.get("share_token")
        if not share_url:
            self.toast("This group has no share link")
            return

        dlg = StandardDialog(title="Share Group", width=360, height=-1)

        def _copy(*_):
            Gdk.Display.get_default().get_clipboard().set(share_url)
            self.toast("Link copied!")
            dlg.close()

        copy_btn = Gtk.Button(label="Copy")
        copy_btn.add_css_class("suggested-action")
        copy_btn.connect("clicked", _copy)
        dlg.add_header_widget(copy_btn, end=True)

        box = dlg.set_scrolled_body(margin=20, spacing=16)

        # Group name
        name_lbl = Gtk.Label(label=esc(group.get("name", "Group")))
        name_lbl.add_css_class("title-2")
        box.append(name_lbl)

        # URL display
        url_row = Adw.ActionRow(title="Invite Link")
        url_row.set_subtitle(share_url)
        url_row.set_subtitle_selectable(True)
        box.append(url_row)

        # System share button (via portal)
        share_btn = Gtk.Button(label="Share…")
        share_btn.add_css_class("pill")
        share_btn.set_icon_name("mail-forward-symbolic")

        def _system_share(*_):
            try:
                # Use Gio to trigger the system share sheet
                Gio.AppInfo.launch_default_for_uri(share_url, None)
            except Exception as e:
                # Don't silently fall back to copy — the user clicked
                # Share, not Copy. Surface the failure and let them
                # retry or use the explicit Copy button.
                log.debug("system share failed: %s", e)
                self.toast("Couldn't open share sheet — try Copy instead")

        share_btn.connect("clicked", _system_share)
        box.append(share_btn)

        # QR code using pure Python (no external library)
        try:
            qr_widget = self._make_qr_widget(share_url)
            if qr_widget:
                sep = Gtk.Separator()
                sep.set_margin_top(4)
                sep.set_margin_bottom(4)
                box.append(sep)
                qr_lbl = Gtk.Label(label="Scan to join")
                qr_lbl.add_css_class("dim-label")
                box.append(qr_lbl)
                box.append(qr_widget)
        except Exception as e:
            log.debug("QR generation failed: %s", e)

        dlg.present(self)

    def _make_qr_widget(self, url: str):
        """Generate a QR code as a DrawingArea using the qrcode library if available,
        or a cairo-drawn pixel grid if not."""
        try:
            import qrcode
            qr = qrcode.QRCode(border=2)
            qr.add_data(url)
            qr.make(fit=True)
            matrix = qr.get_matrix()
        except ImportError:
            # Minimal QR fallback: draw a placeholder with the URL text
            lbl = Gtk.Label(label="Install 'qrcode' package for QR codes\n(pip install qrcode)")
            lbl.add_css_class("dim-label")
            lbl.set_wrap(True)
            lbl.set_justify(Gtk.Justification.CENTER)
            return lbl

        n    = len(matrix)
        size = min(240, 240)
        cell = size // n

        area = Gtk.DrawingArea()
        area.set_size_request(n * cell, n * cell)
        area.set_halign(Gtk.Align.CENTER)

        def _draw(widget, cr, width, height):
            cr.set_source_rgb(1, 1, 1)
            cr.paint()
            cr.set_source_rgb(0, 0, 0)
            for r, row in enumerate(matrix):
                for c, dark in enumerate(row):
                    if dark:
                        cr.rectangle(c * cell, r * cell, cell, cell)
                        cr.fill()

        area.set_draw_func(_draw)
        return area

    def _open_group_settings(self, group: dict = None):
        g = group or self._current_group
        if not g:
            return

        def worker():
            full = self._api.get_group(g["id"])
            GLib.idle_add(lambda: GroupSettingsDialog(
                self._api, full or g,
                self._current_user.get("id"),
                self._config, self
            ).present(self))

        run_in_background(worker)

    def _show_members_panel(self, *_):
        if not self._current_group:
            return

        def worker():
            full = self._api.get_group(self._current_group["id"])
            GLib.idle_add(lambda: MembersDialog(
                self._api, full or self._current_group,
                self._current_user.get("id"),
                self
            ).present(self))

        run_in_background(worker)

    # ── App-level actions ──
    def _on_find(self, *_):
        if self._chat_view is not None:
            self._chat_view.toggle_search()

    def _on_mark_all_read(self, *_):
        """Iterate every row with unread > 0 and POST a per-conversation
        read_receipt for it. We optimistically zero the badge first
        (good UX even if a request fails — the next bg_poll will
        correct any rows whose POST failed) and fan the requests out
        in a single worker thread."""
        if self._api is None:
            return
        targets: list = []   # list of (cid, row)
        for key, row in self._rows.items():
            if getattr(row, "_unread_count_n", 0) <= 0:
                continue
            conv_type, conv_id = key
            if conv_type == "group":
                cid = str(conv_id)
            else:
                # DM — same <lo>+<hi> conversation_id used by the
                # other receipt path.
                me = str((self._current_user or {}).get("id", ""))
                try:
                    a, b = int(me), int(conv_id)
                    lo, hi = (a, b) if a < b else (b, a)
                    cid = f"{lo}+{hi}"
                except (TypeError, ValueError):
                    cid = f"{me}+{conv_id}"
            targets.append((cid, row))

        if not targets:
            self.toast("No unread conversations")
            return

        # Optimistically zero each badge — server-side ack will land
        # within a tick, and the next bg_poll syncs anyway.
        for _cid, row in targets:
            try:
                row.set_unread(0)
            except Exception:
                pass

        api = self._api
        cids = [cid for cid, _ in targets]

        def worker():
            for cid in cids:
                try:
                    api.read_receipt(cid)
                except Exception as e:
                    log.debug("mark-all-read: receipt for %s failed: %s", cid, e)

        run_in_background(worker)
        self.toast(f"Marked {len(targets)} read")
