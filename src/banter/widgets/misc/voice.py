"""VoiceAttachment — inline voice-note player with waveform + save."""

import json
import logging
from datetime import datetime

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ...async_utils import run_in_background
from ...constants import CACHE_DIR
from ...helpers import _cache_key, load_audio_async

log = logging.getLogger(__name__)


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
                try:
                    self._media.seek(0)
                except Exception:
                    pass
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
            log.debug("voice playback init failed: %s", e)
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
            try:
                self._media.seek(0)
            except Exception:
                pass
            self._play_btn.set_icon_name("media-playback-start-symbolic")
            self._dur_label.set_text(self._fmt_dur(self._duration_s))
            self._wave.queue_draw()

    # ── Save / download ──
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

        try:
            self._parent.toast("Downloading voice message…")
        except Exception:
            pass

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
                    log.debug("voice save: cache-copy failed: %s", e)
            if not ok:
                ok = api.download_audio(url, dest)
            def report():
                try:
                    parent.toast(
                        f"Saved to {dest}" if ok else "Save failed")
                except Exception:
                    pass
            GLib.idle_add(report)

        run_in_background(worker)
