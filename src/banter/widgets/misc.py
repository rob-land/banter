"""Banter — miscellaneous reusable widgets (LoadingRow, ImageAttachment, VideoAttachment, VoiceAttachment, FileAttachment, DateSeparator)."""

import json
import urllib.request
from datetime import datetime
from gi.repository import Gtk, Adw, GLib, Gdk, Gio

from ..constants import CACHE_DIR, dbg
from ..async_utils import run_in_background
from ..helpers import (
    load_texture_async, load_audio_async, load_video_async, _cache_key,
)
from .base import StandardDialog


class LoadingRow(Adw.ActionRow):
    def __init__(self, label="Loading…"):
        super().__init__(title=label)
        spinner = Gtk.Spinner(spinning=True)
        self.add_suffix(spinner)
        self.set_activatable(False)


class ImageAttachment(Gtk.Frame):
    """Lazy-loading image widget for message attachments."""
    MAX_W, MAX_H = 280, 200

    def __init__(self, url: str, parent_window):
        super().__init__()
        self.add_css_class("attachment-frame")
        self.parent_window = parent_window
        self._url = url

        self._stack = Gtk.Stack()
        spinner = Gtk.Spinner(spinning=True, margin_top=16,
                               margin_bottom=16, margin_start=16,
                               margin_end=16)
        self._stack.add_named(spinner, "loading")

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_size_request(self.MAX_W, self.MAX_H)
        self._stack.add_named(self._picture, "image")

        err = Gtk.Label(label="⚠ Image unavailable")
        err.add_css_class("dim-label")
        self._stack.add_named(err, "error")

        self._stack.set_visible_child_name("loading")
        self.set_child(self._stack)

        self.set_size_request(self.MAX_W, self.MAX_H)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        gest = Gtk.GestureClick()
        gest.connect("pressed", self._on_click)
        self.add_controller(gest)

        load_texture_async(url, self.MAX_W, self.MAX_H, self._on_loaded)

    def _on_loaded(self, texture):
        if texture is None:
            self._stack.set_visible_child_name("error")
            return
        self._picture.set_paintable(texture)
        self._stack.set_visible_child_name("image")

    def _on_click(self, gest, n, x, y):
        dialog = StandardDialog(title="Image", width=720, height=640)
        dialog.set_follows_content_size(False)

        save_btn = Gtk.Button(icon_name="document-save-symbolic")
        save_btn.set_tooltip_text("Save full-size image")
        save_btn.add_css_class("flat")
        save_btn.connect("clicked", self._save_image, dialog)
        dialog.add_header_widget(save_btn, end=True)

        # Scrolled container lets the user pan very large images and
        # makes the dialog behave sensibly on narrow phone screens.
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_kinetic_scrolling(True)

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_vexpand(True)
        picture.set_hexpand(True)

        cached = CACHE_DIR / f"{_cache_key(self._url)}.img"
        if cached.exists():
            picture.set_filename(str(cached))

        scroll.set_child(picture)
        dialog.set_body(scroll)
        dialog.present(self.parent_window)

    def _save_image(self, btn, dialog):
        fd = Gtk.FileDialog()
        fd.set_title("Save Image")
        # Guess an extension from the URL so the default filename is sensible
        ext = "jpg"
        for e in ("png", "gif", "webp", "jpeg", "jpg"):
            if f".{e}" in self._url.lower():
                ext = "jpeg" if e == "jpg" else e
                break
        fd.set_initial_name(f"groupme_image.{ext}")
        fd.save(self.parent_window, None, self._do_save)

    def _do_save(self, fd, result):
        try:
            file = fd.save_finish(result)
            dest = file.get_path()
            key  = _cache_key(self._url)
            src  = CACHE_DIR / f"{key}.img"
            if src.exists():
                import shutil
                shutil.copy(src, dest)
        except GLib.Error:
            pass


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
        rect.x = int(x); rect.y = int(y)
        rect.width = 1; rect.height = 1
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

        try: self._parent.toast("Downloading video…")
        except Exception: pass

        url    = self._video_url
        parent = self._parent
        # Reuse the playback cache if we already downloaded this clip.
        from ..constants import CACHE_DIR
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
                except Exception: pass
            GLib.idle_add(report)

        run_in_background(worker)


class VoiceAttachment(Gtk.Box):
    """Inline player for `audio` (voice-note) attachments.

    Renders a play/pause button + a 20-bar waveform + a duration label.
    First click downloads the M4A audio (m.groupme.com → cdn2.groupme.com,
    cookie-token-authed redirect handled in `api.download_audio`) into
    the local cache and starts playback via `Gtk.MediaFile`. Subsequent
    clicks toggle play/pause; the waveform fills with the accent color
    behind the playhead position to give a visual progress cue."""

    HEIGHT = 44
    WAVE_W = 180
    WAVE_BARS = 20

    def __init__(self, url: str, duration_s: int,
                 peaks_str, api, parent_window=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("voice-attachment")
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        self._url        = url
        self._duration_s = int(duration_s or 0)
        self._api        = api
        self._parent     = parent_window
        self._media      = None     # Gtk.MediaFile
        self._loading    = False
        self._timer_id   = None

        # `peaks` is delivered as a *stringified* JSON array of floats
        # in [0,1] — parse it once. Fall back to a flat sparkline if
        # the field is missing/malformed so the widget still renders.
        peaks = []
        try:
            if isinstance(peaks_str, str) and peaks_str.strip():
                peaks = json.loads(peaks_str)
            elif isinstance(peaks_str, list):
                peaks = peaks_str
        except Exception:
            peaks = []
        try:
            self._peaks = [max(0.05, min(1.0, float(p)))
                           for p in (peaks or [])][:self.WAVE_BARS]
        except Exception:
            self._peaks = []
        if not self._peaks:
            self._peaks = [0.4] * self.WAVE_BARS

        # Play/pause toggle
        self._play_btn = Gtk.Button(
            icon_name="media-playback-start-symbolic")
        self._play_btn.add_css_class("circular")
        self._play_btn.add_css_class("flat")
        self._play_btn.set_tooltip_text("Play voice message")
        self._play_btn.set_valign(Gtk.Align.CENTER)
        self._play_btn.connect("clicked", self._on_play_clicked)
        self.append(self._play_btn)

        # Waveform
        self._wave = Gtk.DrawingArea()
        self._wave.set_size_request(self.WAVE_W, self.HEIGHT - 10)
        self._wave.set_valign(Gtk.Align.CENTER)
        self._wave.set_draw_func(self._draw_wave)
        self.append(self._wave)

        # Duration / current-position label
        self._dur_label = Gtk.Label(label=self._fmt_dur(self._duration_s))
        self._dur_label.add_css_class("dim-caption")
        self._dur_label.set_valign(Gtk.Align.CENTER)
        self.append(self._dur_label)

        # Right-click / long-press → save menu. Same pattern as
        # VideoAttachment (per-instance ActionGroup so multiple voice
        # bubbles don't fight over a global "voice.save" action).
        ag = Gio.SimpleActionGroup()
        save_act = Gio.SimpleAction.new("save", None)
        save_act.connect("activate", self._save_audio)
        ag.add_action(save_act)
        self.insert_action_group("voice", ag)

        menu = Gio.Menu()
        menu.append("Save Voice Message…", "voice.save")
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

    @staticmethod
    def _fmt_dur(seconds) -> str:
        try:
            seconds = int(round(float(seconds)))
        except (TypeError, ValueError):
            seconds = 0
        m, s = divmod(max(0, seconds), 60)
        return f"{m}:{s:02d}"

    def _accent_rgba(self):
        """Resolve libadwaita's current accent color for the waveform
        played-region fill. Falls back to GNOME default blue on older
        Adwaita that lacks `get_accent_color_rgba`."""
        try:
            return Adw.StyleManager.get_default().get_accent_color_rgba()
        except Exception:
            rgba = Gdk.RGBA()
            rgba.parse("#3584e4")
            return rgba

    def _draw_wave(self, area, cr, w, h):
        n = len(self._peaks)
        if n == 0 or w <= 0 or h <= 0:
            return
        progress = 0.0
        if self._media is not None:
            try:
                d = self._media.get_duration()
                t = self._media.get_timestamp()
                if d > 0:
                    progress = max(0.0, min(1.0, t / d))
            except Exception:
                progress = 0.0

        accent = self._accent_rgba()
        gap = 2.0
        bar_w = max(1.5, (w - gap * (n - 1)) / n)
        progress_x = w * progress
        for i, peak in enumerate(self._peaks):
            bar_h = max(2.0, peak * h)
            x = i * (bar_w + gap)
            y = (h - bar_h) / 2.0
            played = (x + bar_w / 2.0) < progress_x
            if played:
                cr.set_source_rgba(accent.red, accent.green,
                                   accent.blue, 1.0)
            else:
                cr.set_source_rgba(0.55, 0.55, 0.55, 0.55)
            cr.rectangle(x, y, bar_w, bar_h)
            cr.fill()

    def _on_play_clicked(self, *_):
        if self._loading:
            return
        if self._media is None:
            # First press — fetch the audio. Show a busy state on the
            # button while the download lands.
            self._loading = True
            self._play_btn.set_sensitive(False)
            load_audio_async(self._api, self._url, self._on_audio_loaded)
            return
        # Toggle on subsequent presses.
        if self._media.get_playing():
            self._media.pause()
            self._play_btn.set_icon_name("media-playback-start-symbolic")
        else:
            # If playback ran to the end last time, rewind so the next
            # press starts from zero rather than refusing to move.
            if self._media.get_ended():
                try: self._media.seek(0)
                except Exception: pass
            self._media.play()
            self._play_btn.set_icon_name("media-playback-pause-symbolic")
            if self._timer_id is None:
                self._timer_id = GLib.timeout_add(100, self._tick)

    def _on_audio_loaded(self, path):
        self._loading = False
        self._play_btn.set_sensitive(True)
        if not path:
            self._play_btn.set_icon_name("dialog-error-symbolic")
            self._play_btn.set_tooltip_text("Voice message unavailable")
            return
        try:
            f = Gio.File.new_for_path(path)
            self._media = Gtk.MediaFile.new_for_file(f)
            self._media.set_loop(False)
            # `notify::ended` fires when MediaStream's ended property
            # flips true — restore the play icon and kill the timer.
            self._media.connect("notify::ended", self._on_media_ended)
            self._media.play()
            self._play_btn.set_icon_name("media-playback-pause-symbolic")
        except Exception as e:
            dbg("voice playback init failed: %s", e)
            self._play_btn.set_icon_name("dialog-error-symbolic")
            self._play_btn.set_tooltip_text("Voice playback unavailable")
            self._media = None
            return
        if self._timer_id is None:
            self._timer_id = GLib.timeout_add(100, self._tick)

    def _tick(self):
        if self._media is None:
            self._timer_id = None
            return False
        # Update label to current playhead while playing
        try:
            t = self._media.get_timestamp() / 1_000_000
            d = self._media.get_duration() / 1_000_000 \
                if self._media.get_duration() > 0 else self._duration_s
            self._dur_label.set_text(self._fmt_dur(t if t > 0 else d))
        except Exception:
            pass
        self._wave.queue_draw()
        if self._media.get_ended():
            self._timer_id = None
            return False
        if not self._media.get_playing():
            # Paused — keep timer alive so a resume picks up smoothly
            # without the cost of re-arming. The 100 ms tick is cheap.
            return True
        return True

    def _on_media_ended(self, *_):
        if self._media is None:
            return
        if self._media.get_ended():
            try: self._media.seek(0)
            except Exception: pass
            self._play_btn.set_icon_name("media-playback-start-symbolic")
            self._dur_label.set_text(self._fmt_dur(self._duration_s))
            self._wave.queue_draw()

    # ── Save / download ────────────────────────────────────────────
    def _on_secondary(self, _g, _n, x, y):
        self._show_menu_at(x, y)

    def _show_menu_at(self, x, y):
        rect = Gdk.Rectangle()
        rect.x = int(x); rect.y = int(y)
        rect.width = 1; rect.height = 1
        self._popover.set_pointing_to(rect)
        self._popover.popup()

    def _save_audio(self, *_):
        if not self._url or self._parent is None:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fd = Gtk.FileDialog()
        fd.set_title("Save Voice Message")
        fd.set_initial_name(f"groupme-voice-{stamp}.m4a")
        fd.save(self._parent, None, self._on_save_chosen)

    def _on_save_chosen(self, fd, result):
        try:
            f    = fd.save_finish(result)
            dest = f.get_path()
        except GLib.Error:
            return   # user cancelled

        try: self._parent.toast("Downloading voice message…")
        except Exception: pass

        # If we already cached the audio for playback, just copy the
        # cached file — saves a network round-trip and works while
        # offline. Otherwise hit the API (cookie auth + Azure SAS
        # redirect handled there).
        url    = self._url
        api    = self._api
        parent = self._parent
        cached = None
        try:
            cached = str(CACHE_DIR / f"{_cache_key(url)}.audio")
            from pathlib import Path
            if not Path(cached).exists():
                cached = None
        except Exception:
            cached = None

        def worker():
            ok = False
            if cached:
                try:
                    import shutil
                    shutil.copy(cached, dest)
                    ok = True
                except Exception as e:
                    dbg("voice save: cache-copy failed: %s", e)
            if not ok:
                ok = api.download_audio(url, dest)
            def report():
                try:
                    parent.toast(
                        f"Saved to {dest}" if ok else "Save failed")
                except Exception: pass
            GLib.idle_add(report)

        run_in_background(worker)


def _format_size(n: int) -> str:
    """Pretty-print a byte count: 18 KB / 4.2 MB / etc."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    if i == 0:
        return f"{int(f)} B"
    return f"{f:.0f} {units[i]}" if f >= 10 else f"{f:.1f} {units[i]}"


class FileAttachment(Gtk.Button):
    """Pill-style download button for a `{type:file, file_id}` attachment.

    Async-fetches `{file_name, file_size, mime_type}` from the
    fileData endpoint on construction; until that resolves the label
    shows a generic placeholder. Clicking opens the file's download
    URL through the system handler — Banter doesn't manage the
    download itself."""

    def __init__(self, file_id: str, conv_id: str, api, parent_window):
        super().__init__()
        self.add_css_class("file-attachment")
        self.add_css_class("flat")
        self._file_id  = file_id
        # The conversation_id used by file.groupme.com endpoints —
        # group_id for groups, "<lo>+<hi>" for DMs.
        self._cid      = conv_id
        self._api      = api
        self._parent   = parent_window
        self._file_name = ""

        box = Gtk.Box(spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
        icon.set_pixel_size(22)
        box.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._name_lbl = Gtk.Label(label="Loading…", xalign=0)
        self._name_lbl.set_ellipsize(3)   # END
        self._name_lbl.set_max_width_chars(28)
        self._size_lbl = Gtk.Label(label="", xalign=0)
        self._size_lbl.add_css_class("dim-label")
        self._size_lbl.add_css_class("caption")
        text_box.append(self._name_lbl)
        text_box.append(self._size_lbl)
        box.append(text_box)
        self.set_child(box)

        self.connect("clicked", self._on_clicked)
        self._fetch_metadata()

    def _fetch_metadata(self):
        cid, fid = self._cid, self._file_id
        api = self._api
        def worker():
            data = api.get_file_data(cid, [fid])
            GLib.idle_add(self._on_metadata, data.get(fid) or {})
        run_in_background(worker)

    def _on_metadata(self, fd: dict):
        name = fd.get("file_name") or "Attachment"
        size = fd.get("file_size") or 0
        self._file_name = name
        self._name_lbl.set_text(name)
        size_text = _format_size(size)
        if size_text:
            self._size_lbl.set_text(size_text)

    def _on_clicked(self, *_):
        """Tap → save dialog → authenticated download to chosen path.
        Keeps the user in-app; the alternative (handing the URL to
        Gio.AppInfo.launch_default_for_uri) ships them out to the
        system browser, which works but breaks flow."""
        fd = Gtk.FileDialog()
        fd.set_title("Save attachment")
        fd.set_initial_name(self._file_name or "groupme_file")
        fd.save(self._parent, None, self._on_save_chosen)

    def _on_save_chosen(self, fd, result):
        try:
            f    = fd.save_finish(result)
            dest = f.get_path()
        except GLib.Error:
            return   # user cancelled

        name = self._file_name or "file"
        try: self._parent.toast(f"Saving {name}…")
        except Exception: pass

        cid    = self._cid
        fid    = self._file_id
        api    = self._api
        parent = self._parent

        def worker():
            ok = api.download_file(cid, fid, dest)
            def report():
                try:
                    parent.toast(
                        f"Saved to {dest}" if ok else "Download failed")
                except Exception: pass
            GLib.idle_add(report)

        run_in_background(worker)


# ─────────────────────────── Date Separator ──────────────────────

class DateSeparator(Gtk.Box):
    """Centered pill label shown between messages on different calendar days."""

    def __init__(self, d):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class("date-separator")
        self.set_halign(Gtk.Align.CENTER)

        text = self._format(d)
        lbl  = Gtk.Label(label=text)
        lbl.add_css_class("date-separator-label")
        self.append(lbl)

    @staticmethod
    def _format(d) -> str:
        today     = datetime.now().date()
        yesterday = today.__class__.fromordinal(today.toordinal() - 1)
        if d == today:
            return "Today"
        if d == yesterday:
            return "Yesterday"
        # Within the current year: "Monday, January 6"
        if d.year == today.year:
            return d.strftime("%A, %B %-d")
        # Older: "January 6, 2023"
        return d.strftime("%B %-d, %Y")


# ─────────────────────────── Message Bubble ──────────────────────

