"""VoiceMixin — push-to-record voice messages via GStreamer.

Mixed into ChatView. Tap the mic button to start, tap again to stop.
The recorded Opus-in-Ogg file is uploaded via the file-attachment
path so receivers see it as a download; the official client treats
voice messages as a distinct attachment type, but that endpoint
isn't documented.
"""

import logging
import os
import tempfile
from datetime import datetime

from gi.repository import GLib, Gst

from ...async_utils import run_in_background

log = logging.getLogger(__name__)


class VoiceMixin:
    def _toggle_recording(self, *_):
        if self._recording_pipeline is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        Gst.init(None)

        # Temp file in the system tmpdir; we delete=False so the
        # filesink can write to a path that outlives the Python
        # NamedTemporaryFile object.
        f = tempfile.NamedTemporaryFile(
            delete=False, suffix=".ogg", prefix="banter-voice-")
        self._record_path = f.name
        f.close()

        # Opus-in-Ogg keeps the file small and works in any modern
        # GStreamer install. autoaudiosrc picks pulsesrc / pipewiresrc
        # / alsasrc as appropriate for the host.
        pipeline_str = (
            f"autoaudiosrc ! audioconvert ! audioresample ! "
            f"opusenc ! oggmux ! filesink location={self._record_path}"
        )
        try:
            pipeline = Gst.parse_launch(pipeline_str)
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("audio pipeline could not start")
        except Exception as e:
            log.debug("voice record start failed: %s", e)
            self._win.toast(f"Recording failed: {e}")
            try:
                os.unlink(self._record_path)
            except Exception:
                pass
            self._record_path = None
            return

        self._recording_pipeline = pipeline
        self._mic_btn.set_icon_name("media-playback-stop-symbolic")
        self._mic_btn.add_css_class("destructive-action")
        self._mic_btn.set_tooltip_text("Stop recording")
        self._win.toast("Recording…")

    def _stop_recording(self):
        pipeline = self._recording_pipeline
        if pipeline is None:
            return
        self._recording_pipeline = None

        self._mic_btn.set_icon_name("audio-input-microphone-symbolic")
        self._mic_btn.remove_css_class("destructive-action")
        self._mic_btn.set_tooltip_text("Record voice message")

        path = self._record_path
        self._record_path = None
        cid  = self._conv_id()
        api  = self._api
        win  = self._win

        self._win.toast("Saving voice message…")

        def worker():
            # Send EOS so the muxer finalises the OGG headers, then
            # wait briefly for the bus to confirm before tearing the
            # pipeline down.
            bus = pipeline.get_bus()
            pipeline.send_event(Gst.Event.new_eos())
            bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS)
            pipeline.set_state(Gst.State.NULL)

            file_id = api.upload_file(cid, path)
            try:
                os.unlink(path)
            except Exception:
                pass

            if file_id:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                name  = f"voice-{stamp}.ogg"
                GLib.idle_add(self._set_pending_file, file_id, name)
            else:
                GLib.idle_add(lambda: win.toast("Voice upload failed"))

        run_in_background(worker)
