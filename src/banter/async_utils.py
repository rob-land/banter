"""Banter — instance-owned background runner for sync HTTP / blocking I/O.

The Application owns one ``BackgroundRunner``; the rest of the codebase
submits sync callables through it. Workers run on a shared
``ThreadPoolExecutor`` (so we don't spawn a fresh OS thread per call);
results land back on the GTK main loop via ``GLib.idle_add`` so
callbacks may touch widgets safely.

Banter sticks with sync ``urllib`` for HTTP rather than pulling in
``aiohttp`` or Soup3, so this is the right shape for the cohort: an
instance-owned executor pool, not an asyncio loop.

The module-level ``run_in_background`` shim is kept for ergonomics —
it delegates to ``Adw.Application.get_default().runner`` so existing
call sites don't have to learn a new API.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from gi.repository import Adw, GLib

log = logging.getLogger(__name__)


class BackgroundRunner:
    """A shared thread pool for sync work.

    One per Application. Built in ``do_startup`` and shut down in
    ``do_shutdown``. Submit zero-arg callables with ``submit(worker,
    on_done=…, on_error=…)``; callbacks fire on the GTK main loop.
    """

    def __init__(self, max_workers: int = 8, name: str = "banter-bg") -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=name,
        )

    def submit(
        self,
        worker: Callable[[], Any],
        on_done: Callable[[Any], Any] | None = None,
        on_error: Callable[[BaseException], Any] | None = None,
    ) -> Future:
        """Run ``worker()`` on the pool. ``on_done`` / ``on_error``
        callbacks (optional) are scheduled on the GTK main loop.

        When ``on_error`` is omitted the exception is logged and
        swallowed — the caller has no UI contract to keep consistent.
        """
        def _wrapped() -> Any:
            try:
                result = worker()
            except Exception as exc:
                log.exception("background worker raised")
                if on_error is not None:
                    GLib.idle_add(_invoke_one, on_error, exc)
                return None
            if on_done is not None:
                GLib.idle_add(_invoke_one, on_done, result)
            return result
        return self._executor.submit(_wrapped)

    def stop(self, timeout: float = 2.0) -> None:
        """Shut the pool down. Pending tasks are cancelled where
        possible; in-flight workers finish on their own (they're
        daemon threads so the process can still exit)."""
        self._executor.shutdown(wait=False, cancel_futures=True)


# ── module-level shim (delegates to the active Application's runner) ──

def _current() -> BackgroundRunner:
    app = Adw.Application.get_default()
    if app is None or not hasattr(app, "runner"):
        raise RuntimeError(
            "async_utils used before BackgroundRunner started; "
            "ensure Application.do_startup creates self.runner"
        )
    return app.runner


def run_in_background(
    worker: Callable[[], Any],
    on_done: Callable[[Any], Any] | None = None,
    on_error: Callable[[BaseException], Any] | None = None,
) -> Future:
    """Module-level shim for back-compat. New code should grab the
    runner off the application directly (cleaner, testable)."""
    return _current().submit(worker, on_done=on_done, on_error=on_error)


# ── internals ──────────────────────────────────────────────────────────


def _invoke_one(callback: Callable[[Any], Any], arg: Any) -> bool:
    try:
        callback(arg)
    except BaseException:
        log.exception("idle callback failed")
    return False  # don't reschedule
