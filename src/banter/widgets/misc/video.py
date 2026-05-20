"""VideoAttachment — click-to-stream inline video player with right-click
save."""

import urllib.request

from gi.repository import Gdk, Gio, GLib, Gtk

from ...async_utils import run_in_background
from ...helpers import _cache_key, load_texture_async, load_video_async


class VideoAttachment(Gtk.Overlay):
    """Inline video player for GroupMe `video` attachments.

    Shows the preview thumbnail with a centered play button. The
    network is left alone until the user clicks: at that point the
    thumbnail is swapped for a Gtk.Video that streams from `url`.
    Keeps the chat scroll cheap when there are many videos in view.

    Right-click / long-press opens a popover with "Save Video…"
    which downloads the URL to a user-chosen path."""

    MAX_W, MAX_H = 320, 240

    def __init__(self, video_url: str, preview_url: str = "",
                 parent_window=None):
        super().__init__()
        self.add_css_class("attachment-frame")
        self._video_url = video_url
        self._parent    = parent_window
        self._playing   = False

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.set_size_request(self.MAX_W, self.MAX_H)
        self.set_child(self._picture)

        self._play_overlay = Gtk.Image.new_from_icon_name(
            "media-playback-start-symbolic")
        self._play_overlay.set_pixel_size(56)
        self._play_overlay.set_halign(Gtk.Align.CENTER)
        self._play_overlay.set_valign(Gtk.Align.CENTER)
        self._play_overlay.add_css_class("video-play-overlay")
        self.add_overlay(self._play_overlay)

        gest = Gtk.GestureClick()
        gest.connect("pressed", self._on_click)
        self.add_controller(gest)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        # Right-click / long-press → save menu. Action group is
        # scoped to this widget so multiple videos in the same chat
        # don't collide on one global "video.save" action.
        ag = Gio.SimpleActionGroup()
        save_act = Gio.SimpleAction.new("save", None)
        save_act.connect("activate", self._save_video)
        ag.add_action(save_act)
        self.insert_action_group("video", ag)

        menu = Gio.Menu()
        menu.append("Save Video…", "video.save")
        self._popover = Gtk.PopoverMenu.new_from_model(menu)
        self._popover.set_parent(self)
        self._popover.set_has_arrow(False)

        rclick = Gtk.GestureClick.new()
        rclick.set_button(Gdk.BUTTON_SECONDARY)
        rclick.connect("pressed", self._on_secondary)
        self.add_controller(rclick)

        lp = Gtk.GestureLongPress.new()
        lp.set_touch_only(True)
        lp.connect("pressed",
            lambda _g, x, y: self._show_menu_at(x, y))
        self.add_controller(lp)

        if preview_url:
            load_texture_async(preview_url, self.MAX_W, self.MAX_H,
                               self._on_preview_loaded)

    def _on_preview_loaded(self, texture):
        if texture is None or self._playing:
            return
        self._picture.set_paintable(texture)

    def _on_click(self, *_):
        if self._playing or not self._video_url:
            return
        self._playing = True
        # Swap the play icon for a spinner while the clip downloads.
        # `Gtk.Video.set_file` on a remote URI was failing with
        # `gtk_gst_media_file_source_setup_cb: assertion 'stream != NULL'`
        # — playback from a local file is materially more reliable.
        try:
            self._play_overlay.set_from_icon_name(
                "content-loading-symbolic")
        except Exception:
            pass
        self.set_cursor(None)
        load_video_async(self._video_url, self._on_video_ready)

    def _on_video_ready(self, path):
        if not path:
            try:
                self._play_overlay.set_from_icon_name(
                    "dialog-warning-symbolic")
            except Exception:
                pass
            self._playing = False
            return
        video = Gtk.Video()
        video.set_size_request(self.MAX_W, self.MAX_H)
        video.set_autoplay(True)
        video.set_filename(path)
        self.set_child(video)
        try:
            self.remove_overlay(self._play_overlay)
        except Exception:
            pass

    def _on_secondary(self, _g, _n, x, y):
        self._show_menu_at(x, y)

    def _show_menu_at(self, x, y):
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _save_video(self, *_):
        if not self._video_url or self._parent is None:
            return
        # Pick an extension from the URL so the default name is sensible.
        ext = "mp4"
        for e in ("mp4", "mov", "webm", "mkv", "m4v"):
            if f".{e}" in self._video_url.lower():
                ext = e
                break
        fd = Gtk.FileDialog()
        fd.set_title("Save Video")
        fd.set_initial_name(f"groupme_video.{ext}")
        fd.save(self._parent, None, self._on_save_chosen)

    def _on_save_chosen(self, fd, result):
        try:
            f    = fd.save_finish(result)
            dest = f.get_path()
        except GLib.Error:
            return   # user cancelled

        try:
            self._parent.toast("Downloading video…")
        except Exception:
            pass

        url    = self._video_url
        parent = self._parent
        # Reuse the playback cache if we already downloaded this clip.
        from ...constants import CACHE_DIR
        cached = CACHE_DIR / f"{_cache_key(url)}.video"

        def worker():
            try:
                if cached.exists():
                    import shutil
                    shutil.copy(cached, dest)
                else:
                    urllib.request.urlretrieve(url, dest)
                ok = True
            except Exception:
                ok = False
            def report():
                try:
                    parent.toast(
                        f"Saved to {dest}" if ok else "Save failed")
                except Exception:
                    pass
            GLib.idle_add(report)

        run_in_background(worker)
