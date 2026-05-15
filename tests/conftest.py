"""Shared pytest fixtures + the mock-urlopen helper used by api tests.

`banter.api` is a thin synchronous wrapper over `urllib.request.urlopen`;
mocking that one call is enough to drive every endpoint. We don't need
a real HTTP server.

`banter.const` is meson-generated; stub it at module-load time so tests
can run before / without `meson install`.
"""

from __future__ import annotations

import io
import json
import sys
import types
from typing import Iterable
from unittest.mock import MagicMock

import pytest


# --- banter.const stub --------------------------------------------------

def _ensure_const_stub() -> None:
    if "banter.const" in sys.modules:
        return
    try:
        import banter.const  # noqa: F401
        return
    except Exception:
        pass
    m = types.ModuleType("banter.const")
    m.APP_ID = "land.rob.banter"
    m.APP_NAME = "Banter"
    m.VERSION = "test"
    m.PKGDATADIR = ""
    m.LOCALEDIR = ""
    sys.modules["banter.const"] = m


_ensure_const_stub()


# --- mock urlopen -------------------------------------------------------

class _FakeResp:
    """Mimics the file-like object that `urlopen` returns when used as a
    context manager: `.read()`, `.status`, plus the unused-by-banter
    attributes the urllib internals poke at."""

    def __init__(self, body: bytes, status: int) -> None:
        self._buf = io.BytesIO(body)
        self.status = status
        self.headers = {}

    def read(self, *args, **kwargs) -> bytes:
        return self._buf.read(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._buf.close()
        return False


class MockHTTP:
    """Records request invocations and returns canned responses in
    order. Behaves like a list — `push_*` queues responses, the magic
    `__call__` pops them as urlopen invocations happen.

    `push_json(status, body)` is the common path; `push_http_error`
    raises an HTTPError mid-request (used to drive retry / 401 paths);
    `push_url_error` raises a connection-failed style URLError (used
    to drive offline transitions)."""

    def __init__(self) -> None:
        self.calls: list = []
        self._responses: list = []

    def push_json(self, body, status: int = 200) -> None:
        self._responses.append(("ok", json.dumps(body).encode("utf-8"), status))

    def push_empty(self, status: int = 204) -> None:
        self._responses.append(("ok", b"", status))

    def push_http_error(self, status: int, body=None) -> None:
        payload = json.dumps(body).encode() if body is not None else b""
        self._responses.append(("http_error", status, payload))

    def push_url_error(self, reason: str = "network is down") -> None:
        self._responses.append(("url_error", reason))

    def __call__(self, request, body=None, timeout=None):
        # Capture what banter built before deciding the response. This
        # gives tests access to: method, full URL (incl. token param),
        # all headers, and the request body.
        try:
            url = request.full_url
        except AttributeError:
            url = request.get_full_url()
        captured = {
            "method": request.get_method(),
            "url": url,
            "headers": dict(request.headers),
            "body": body,
            "timeout": timeout,
        }
        self.calls.append(captured)

        if not self._responses:
            pytest.fail(
                f"MockHTTP got an unexpected request: {captured['method']} {url}; "
                f"queue empty"
            )
        head, *rest = self._responses[0], *self._responses[1:]
        self._responses = rest
        kind = head[0]
        if kind == "ok":
            _, raw, status = head
            return _FakeResp(raw, status)
        if kind == "http_error":
            import urllib.error
            _, status, payload = head
            err = urllib.error.HTTPError(url, status, "stub", {}, io.BytesIO(payload))
            err.read = lambda: payload
            raise err
        if kind == "url_error":
            import urllib.error
            _, reason = head
            raise urllib.error.URLError(reason)
        raise AssertionError(f"unknown response kind: {kind}")


@pytest.fixture
def http(monkeypatch) -> MockHTTP:
    """Replace `banter.api.urllib.request.urlopen` with a MockHTTP for
    the duration of one test. Returns the MockHTTP so the test can
    queue responses + assert on captured calls.
    """
    import banter.api as api_mod
    mock = MockHTTP()
    monkeypatch.setattr(api_mod.urllib.request, "urlopen", mock)
    # The other modules that import urlopen (helpers.py for image
    # cache) read it through `urllib.request` too — patching the
    # canonical module-level binding keeps them consistent.
    monkeypatch.setattr("urllib.request.urlopen", mock)
    # Make retries instant so we don't `time.sleep` through the
    # backoff in tests.
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    return mock
