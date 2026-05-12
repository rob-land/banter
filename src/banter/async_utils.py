"""Banter — shared helpers for running background work safely.

All GTK widget mutations must happen on the main thread. The pattern
used across Banter is: spawn a daemon thread that does the HTTP /
file-system work, then `GLib.idle_add` a callback that updates the
UI. This module factors that pattern into one place so call sites
don't have to repeat the thread bookkeeping.
"""

import threading

from gi.repository import GLib

import logging

log = logging.getLogger(__name__)


def run_in_background(worker, on_done=None, on_error=None):
    """Run `worker()` on a daemon thread; schedule the result on the
    main thread.

    - `worker` is a zero-arg callable. Its return value is passed to
      `on_done` via `GLib.idle_add`.
    - `on_done(result)` (optional) is invoked on the main thread if
      `worker` returns normally.
    - `on_error(exc)` (optional) is invoked on the main thread if
      `worker` raises. When omitted, the exception is logged and
      swallowed — the caller has no UI contract to keep consistent.

    Usage:
        def load():
            return api.get_messages(gid)
        run_in_background(load, on_done=self._on_messages)
    """
    def _wrapped():
        try:
            result = worker()
        except Exception as exc:
            log.exception("run_in_background: worker raised")
            if on_error is not None:
                GLib.idle_add(on_error, exc)
            return
        if on_done is not None:
            GLib.idle_add(on_done, result)

    threading.Thread(target=_wrapped, daemon=True).start()
