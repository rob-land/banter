"""SaveActionsMixin — right-click "Save Photo/Video/Voice/Attachment"
plus the "Add to Album…" group-only action.

Each method assumes `self.msg`, `self.api`, `self.win`, and
`self._conversation_id()` are reachable on the host class
(MessageBubble).
"""

import shutil
import urllib.request
from datetime import datetime

from gi.repository import GLib, Gtk

from ...async_utils import run_in_background
from ...constants import CACHE_DIR
from ...helpers import _cache_key


class SaveActionsMixin:
    def _first_attachment_url(self, kind: str) -> str:
        """Return the `url` field of the first attachment of `kind`,
        or '' if the bubble has no such attachment. Used by the
        save-photo / save-video / save-voice menu actions to pick
        the attachment to download."""
        for a in (self.msg.get("attachments") or []):
            if a.get("type") == kind:
                return a.get("url") or ""
        return ""

    @staticmethod
    def _ext_from_url(url: str, fallback: str) -> str:
        """Best-effort file extension from the path component of `url`.
        Returns `fallback` (with leading dot, e.g. '.m4a') when the URL
        has no usable suffix or one that looks implausible after
        stripping the query string."""
        try:
            path = url.split("?", 1)[0]
            dot   = path.rfind(".")
            slash = path.rfind("/")
            if dot > slash >= 0 and dot < len(path) - 1:
                ext = path[dot:]
                if len(ext) <= 6 and ext[1:].isalnum():
                    return ext
        except Exception:
            pass
        return fallback

    def _action_save_photo(self, *_):
        url = self._first_attachment_url("image")
        if not url:
            return
        ext = self._ext_from_url(url, ".jpg")
        self._save_url_via_dialog(
            url, f"groupme-photo{ext}", "Save Photo",
            authed=False, downloading_msg="Downloading photo…")

    def _action_save_video(self, *_):
        url = self._first_attachment_url("video")
        if not url:
            return
        ext = self._ext_from_url(url, ".mp4")
        self._save_url_via_dialog(
            url, f"groupme-video{ext}", "Save Video",
            authed=False, downloading_msg="Downloading video…")

    def _action_add_to_album(self, *_):
        """Open the album picker pre-loaded with this message's image
        and video attachments. Group-only — the menu predicate already
        gates on this, but a defensive recheck here keeps the handler
        safe if someone activates the action via keyboard / D-Bus."""
        atts = self.msg.get("attachments") or []
        media: list = []
        for a in atts:
            kind = a.get("type")
            if kind not in ("image", "video"):
                continue
            url = a.get("url") or ""
            if not url:
                continue
            media.append({
                "media_url":    url,
                "media_type":   kind,
                "media_source": "album",
            })
        if not media:
            return

        cv = getattr(self.win, "_chat_view", None)
        if cv is None or getattr(cv, "_is_dm", False):
            return
        group = getattr(cv, "_group", None)
        if not isinstance(group, dict):
            return

        # Late import — gallery package pulls in widgets and would
        # circle back if imported at module-load time.
        from ...dialogs.gallery import AlbumPickerDialog
        AlbumPickerDialog(
            self.api, group, media, self.win).present(self.win)

    def _action_save_file(self, *_):
        """Save a `type:"file"` attachment — anything uploaded via the
        file picker (docs, video files, archives). Unlike photo/video
        which carry a public URL on the attachment, files require
        `api.download_file(cid, file_id, dest)` and the original
        filename comes from a separate `get_file_data` lookup."""
        fid = ""
        for a in (self.msg.get("attachments") or []):
            if a.get("type") == "file":
                fid = a.get("file_id") or ""
                break
        if not fid:
            return
        cid = self._conversation_id()
        api = self.api
        win = self.win
        if win is None:
            return
        try:
            win.toast("Preparing download…")
        except Exception:
            pass

        def fetch_meta():
            data = api.get_file_data(cid, [fid])
            meta = data.get(fid) or {}
            GLib.idle_add(self._show_save_file_dialog, cid, fid, meta)

        run_in_background(fetch_meta)

    def _show_save_file_dialog(self, cid: str, fid: str, meta: dict):
        win  = self.win
        api  = self.api
        name = meta.get("file_name") or f"groupme-file-{fid}"
        fd = Gtk.FileDialog()
        fd.set_title("Save Attachment")
        fd.set_initial_name(name)

        def on_chosen(fd, result):
            try:
                f    = fd.save_finish(result)
                dest = f.get_path()
            except GLib.Error:
                return   # user cancelled
            try:
                win.toast(f"Downloading {name}…")
            except Exception:
                pass

            def worker():
                ok = api.download_file(cid, fid, dest)

                def report():
                    try:
                        win.toast(
                            f"Saved to {dest}" if ok else "Save failed")
                    except Exception:
                        pass
                GLib.idle_add(report)

            run_in_background(worker)

        fd.save(win, None, on_chosen)

    def _action_save_voice(self, *_):
        url = self._first_attachment_url("audio")
        if not url:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Reuse the cached copy from playback if it's already on disk
        # — saves a round-trip and works offline.
        cached = ""
        try:
            p = CACHE_DIR / f"{_cache_key(url)}.audio"
            if p.exists():
                cached = str(p)
        except Exception:
            cached = ""
        self._save_url_via_dialog(
            url, f"groupme-voice-{stamp}.m4a", "Save Voice Message",
            authed=True, downloading_msg="Downloading voice message…",
            cached_path=cached)

    def _save_url_via_dialog(self, url: str, default_name: str,
                              title: str, authed: bool,
                              downloading_msg: str,
                              cached_path: str = ""):
        """Show a save-file dialog and stream `url` to the chosen path
        in a worker thread.

        `authed=True` routes through `api.download_audio` which sends
        the m.groupme.com Cookie token. Photo and video URLs are
        public so they go through plain `urlretrieve`. `cached_path`,
        when present, short-circuits the download with a local copy."""
        win = self.win
        if win is None or not url:
            return
        fd = Gtk.FileDialog()
        fd.set_title(title)
        fd.set_initial_name(default_name)

        api = self.api

        def on_chosen(fd, result):
            try:
                f    = fd.save_finish(result)
                dest = f.get_path()
            except GLib.Error:
                return   # user cancelled
            try:
                win.toast(downloading_msg)
            except Exception:
                pass

            def worker():
                ok = False
                if cached_path:
                    try:
                        shutil.copy(cached_path, dest)
                        ok = True
                    except Exception:
                        ok = False
                if not ok:
                    if authed:
                        ok = api.download_audio(url, dest)
                    else:
                        try:
                            urllib.request.urlretrieve(url, dest)
                            ok = True
                        except Exception:
                            ok = False

                def report():
                    try:
                        win.toast(
                            f"Saved to {dest}" if ok else "Save failed")
                    except Exception:
                        pass
                GLib.idle_add(report)

            run_in_background(worker)

        fd.save(win, None, on_chosen)
