"""GroupMe `_req` — request shape, retry, online / unauthorized hooks.

`_req` is the single funnel for every API call in banter. Wire-format
regressions here break the entire app silently; a 401 that doesn't
fire `on_unauthorized` leaves the session-expired prompt hidden;
a missing emoji-friendly body encoding (`ensure_ascii=False`)
silently mojibakes outbound messages.
"""
import urllib.parse

import pytest

from banter.api import GroupMeAPI


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# --- URL + headers -----------------------------------------------------

def test_request_appends_token_as_query_param(http):
    http.push_json({"meta": {"code": 200}, "response": {}})
    api = GroupMeAPI(token="t-abc")
    api._req("GET", "/users/me")

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["method"] == "GET"
    assert _query(call["url"]) == {"token": "t-abc"}
    assert call["url"].startswith("https://api.groupme.com/v3/users/me")


def test_request_sends_token_as_header_too(http):
    """GroupMe's API accepts the token via query param or X-Access-Token;
    banter sends both because the web client does. Catching either path
    drift would alert us before a server-side validation change bites."""
    http.push_json({"meta": {"code": 200}})
    api = GroupMeAPI(token="t-abc")
    api._req("GET", "/users/me")
    assert http.calls[0]["headers"]["X-access-token"] == "t-abc"


def test_request_sets_groupme_web_user_agent(http):
    """The reactions endpoint silently rejects non-heart reactions
    unless `X-Requested-With: GroupMeWeb/...` is set. Pin that header."""
    http.push_json({"meta": {"code": 200}})
    api = GroupMeAPI(token="t")
    api._req("GET", "/groups")
    assert http.calls[0]["headers"]["X-requested-with"].startswith("GroupMeWeb/")


def test_request_merges_params_with_token(http):
    http.push_json({"meta": {"code": 200}, "response": []})
    api = GroupMeAPI(token="t-abc")
    api._req("GET", "/groups", params={"page": 1, "per_page": 20})

    q = _query(http.calls[0]["url"])
    assert q == {"page": "1", "per_page": "20", "token": "t-abc"}


def test_request_uses_alternate_base_url(http):
    """The `edit_message` path POSTs to the v4 API even though the
    rest of banter uses v3. The base= override is the seam — pin it."""
    http.push_json({"meta": {"code": 200}, "response": {}})
    api = GroupMeAPI(token="t")
    api._req("PUT", "/groups/G1/messages/M1",
             {"text": "hi"},
             base="https://api.groupme.com/v4")
    assert http.calls[0]["url"].startswith("https://api.groupme.com/v4/groups/G1/messages/M1")


# --- body encoding -----------------------------------------------------

def test_post_body_is_compact_utf8_json(http):
    """`ensure_ascii=False` so emoji and non-ASCII go on the wire as raw
    UTF-8 bytes. If banter ever defaults back to \\uXXXX escapes,
    GroupMe still accepts the message but other clients render it as
    the literal `\\u`-escaped string — embarrassing and silent."""
    http.push_json({"meta": {"code": 201}, "response": {"message": {}}})
    api = GroupMeAPI(token="t")
    api._req("POST", "/groups/G1/messages",
             {"message": {"text": "hi 👋", "source_guid": "s"}})

    sent = http.calls[0]["body"].decode("utf-8")
    assert "👋" in sent
    assert "\\u" not in sent  # no escaped escapes
    # Content-Type header is also part of the contract.
    assert http.calls[0]["headers"]["Content-type"] == "application/json"


def test_get_request_has_no_body(http):
    http.push_json({"meta": {"code": 200}})
    api = GroupMeAPI(token="t")
    api._req("GET", "/users/me")
    assert http.calls[0]["body"] is None


# --- response shapes ---------------------------------------------------

def test_204_no_content_synthesises_meta_wrapper(http):
    """DELETE endpoints return 204 with an empty body. _req must
    synthesise the `{meta, response}` wrapper so the higher-level
    methods can do their usual `_ok(r)` check."""
    http.push_empty(status=204)
    api = GroupMeAPI(token="t")
    r = api._req("DELETE", "/groups/G1/messages/M1")
    assert r == {"meta": {"code": 204}, "response": None}


def test_json_body_parsed(http):
    http.push_json({"meta": {"code": 200}, "response": {"id": "u1"}})
    api = GroupMeAPI(token="t")
    r = api._req("GET", "/users/me")
    assert r["response"]["id"] == "u1"


# --- retry logic -------------------------------------------------------

@pytest.mark.parametrize("transient", [502, 503, 504])
def test_get_retries_on_transient_5xx(http, transient):
    """5xx in `RETRY_HTTP_CODES` triggers a retry — important for the
    background notifier loop that polls every few seconds."""
    http.push_http_error(transient)
    http.push_json({"meta": {"code": 200}, "response": []})

    api = GroupMeAPI(token="t")
    r = api._req("GET", "/groups")
    assert r["meta"]["code"] == 200
    assert len(http.calls) == 2  # initial + 1 retry


def test_post_does_not_retry_on_5xx(http):
    """Writes use `source_guid` for server-side dedup on /messages, but
    likes/pins don't. Don't gamble on retrying mutations."""
    http.push_http_error(503, {"meta": {"code": 503}})

    api = GroupMeAPI(token="t")
    r = api._req("POST", "/groups/G/messages",
                 {"message": {"text": "x", "source_guid": "g"}})
    assert r["meta"]["code"] == 503
    assert len(http.calls) == 1  # no retry


def test_retry_gives_up_after_max_attempts(http):
    """`RETRY_BACKOFF_S` defines the retry budget. After the last
    attempt the failure is surfaced to the caller."""
    http.push_http_error(503)
    http.push_http_error(503)
    http.push_http_error(503)

    api = GroupMeAPI(token="t")
    r = api._req("GET", "/groups")
    assert r["meta"]["code"] == 503
    # Initial + len(RETRY_BACKOFF_S) retries = 3 total.
    assert len(http.calls) == 3


# --- on_unauthorized hook ----------------------------------------------

def test_401_fires_on_unauthorized_once(http):
    """A 401 means the token is dead. Banter fires the callback so the
    UI can route the user to the re-auth flow. Multiple 401s on the
    same client must only fire once — otherwise the session-expired
    dialog stacks up."""
    fired = []
    http.push_http_error(401, {"meta": {"code": 401}})
    http.push_http_error(401, {"meta": {"code": 401}})

    api = GroupMeAPI(token="t", on_unauthorized=lambda: fired.append(True))
    api._req("GET", "/users/me")
    api._req("GET", "/groups")
    assert fired == [True]


# --- online / offline transitions --------------------------------------

def test_url_error_fires_on_offline(http):
    """A network-level failure (DNS, RST, timeout) flips us offline.
    GETs retry through URL errors too, so we need to exhaust the
    retry budget — push one error per attempt."""
    offline = []
    online = []
    # GroupMeAPI.RETRY_BACKOFF_S has 2 entries → 3 attempts total.
    for _ in range(3):
        http.push_url_error("dns lookup failed")
    api = GroupMeAPI(
        token="t",
        on_online=lambda: online.append(True),
        on_offline=lambda: offline.append(True),
    )
    api._req("GET", "/users/me")
    assert offline == [True]
    assert online == []


def test_offline_fires_only_once_per_transition(http):
    """`_fire_offline` is called from each failing retry attempt but
    must only fire `on_offline` once per offline→online cycle."""
    offline = []
    for _ in range(3):
        http.push_url_error("dns lookup failed")
    api = GroupMeAPI(token="t", on_offline=lambda: offline.append(True))
    api._req("GET", "/users/me")
    assert offline == [True]  # not 3


def test_successful_request_after_offline_fires_on_online(http):
    """Transitions only — `on_online` fires once per offline→online
    crossing, not every successful request."""
    offline = []
    online = []
    # First call: exhaust retries with URLErrors → flip offline.
    for _ in range(3):
        http.push_url_error("dns lookup failed")
    # Second call: succeeds → flip online once.
    http.push_json({"meta": {"code": 200}})
    # Third call: still online → no extra fire.
    http.push_json({"meta": {"code": 200}})

    api = GroupMeAPI(
        token="t",
        on_online=lambda: online.append(True),
        on_offline=lambda: offline.append(True),
    )
    api._req("GET", "/users/me")  # offline
    api._req("GET", "/users/me")  # recovers — fires on_online once
    api._req("GET", "/users/me")  # still online — no extra fire
    assert offline == [True]
    assert online == [True]
