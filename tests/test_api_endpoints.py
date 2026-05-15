"""High-level API methods — verify_token, get_groups, send_message,
edit_message.

These are thin wrappers around `_req` but each has a quirk worth
pinning:
  * verify_token must clear `self.token` on rejection (so a stale
    token doesn't leak into the next call);
  * get_groups must include `include=unread_count` or the sidebar
    badge falls to None;
  * send_message must generate a `source_guid` when the caller
    didn't supply one (server-side dedup key on retry);
  * edit_message uses the **v4** API (the only call that does)
    and the body is unwrapped (no `message` key).
"""

import urllib.parse

import pytest

from banter.api import GroupMeAPI


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _path(url):
    return urllib.parse.urlsplit(url).path


# --- verify_token ------------------------------------------------------

def test_verify_token_success_returns_user(http):
    http.push_json({
        "meta": {"code": 200},
        "response": {"id": "u1", "name": "Alice"},
    })
    api = GroupMeAPI()
    ok, value = api.verify_token("real-token")
    assert ok is True
    assert value == {"id": "u1", "name": "Alice"}
    assert api.token == "real-token"


def test_verify_token_failure_clears_token(http):
    """A 401 from `/users/me` means the candidate token is bad —
    clear `self.token` so a subsequent call doesn't reuse it."""
    http.push_http_error(401, {"meta": {"code": 401, "errors": ["unauthorized"]}})
    api = GroupMeAPI()
    ok, errs = api.verify_token("bogus")
    assert ok is False
    assert errs == ["unauthorized"]
    assert api.token is None


def test_verify_token_strips_whitespace(http):
    """User-pasted tokens often have trailing whitespace. Strip
    before sending so the API doesn't reject with a confusing
    "invalid token" error."""
    http.push_json({"meta": {"code": 200}, "response": {"id": "u1"}})
    api = GroupMeAPI()
    api.verify_token("  padded-token\n")
    assert api.token == "padded-token"


# --- get_groups --------------------------------------------------------

def test_get_groups_requests_unread_count_include(http):
    """`include=unread_count` is what makes the server return real
    integers in `messages.unread_count` and `last_read_*`. Without
    it the sidebar unread badge is broken."""
    http.push_json({"meta": {"code": 200}, "response": []})
    api = GroupMeAPI(token="t")
    api.get_groups()

    q = _query(http.calls[0]["url"])
    assert q["include"] == "unread_count"
    assert q["page"] == "1"
    assert q["per_page"] == "20"
    assert q["omit"] == "memberships"


def test_get_groups_all_paginates_until_short_batch(http):
    """`get_groups_all` keeps walking pages while batches are
    `per_page`-sized and stops on the first short batch."""
    # Page 1: 20 items (full)
    http.push_json({"meta": {"code": 200}, "response": [{"id": str(i)} for i in range(20)]})
    # Page 2: 5 items (partial → stop)
    http.push_json({"meta": {"code": 200}, "response": [{"id": str(20 + i)} for i in range(5)]})

    api = GroupMeAPI(token="t")
    groups = api.get_groups_all()
    assert len(groups) == 25
    assert len(http.calls) == 2

    page1_q = _query(http.calls[0]["url"])
    page2_q = _query(http.calls[1]["url"])
    assert page1_q["page"] == "1"
    assert page2_q["page"] == "2"


def test_get_groups_all_stops_on_first_partial(http):
    http.push_json({"meta": {"code": 200}, "response": [{"id": "g1"}]})
    api = GroupMeAPI(token="t")
    groups = api.get_groups_all()
    assert len(groups) == 1
    assert len(http.calls) == 1


# --- send_message ------------------------------------------------------

def test_send_message_wraps_in_message_envelope(http):
    """The /messages endpoint expects `{message: {text, source_guid, attachments}}`."""
    http.push_json({"meta": {"code": 201}, "response": {"message": {"id": "m1"}}})

    api = GroupMeAPI(token="t")
    api.send_message("G1", "hello", source_guid="custom-guid")

    import json
    body = json.loads(http.calls[0]["body"])
    assert body == {"message": {"source_guid": "custom-guid", "text": "hello"}}
    assert _path(http.calls[0]["url"]) == "/v3/groups/G1/messages"


def test_send_message_generates_source_guid_when_omitted(http):
    """source_guid is the server-side dedup key. Auto-generate if
    the caller didn't supply one — otherwise a network-error retry
    risks sending the same message twice."""
    http.push_json({"meta": {"code": 201}, "response": {"message": {"id": "m1"}}})
    api = GroupMeAPI(token="t")
    api.send_message("G1", "hi")

    import json
    body = json.loads(http.calls[0]["body"])
    assert "source_guid" in body["message"]
    assert body["message"]["source_guid"]  # truthy / non-empty


def test_send_message_attaches_image_payloads(http):
    http.push_json({"meta": {"code": 201}, "response": {"message": {"id": "m1"}}})
    api = GroupMeAPI(token="t")
    api.send_message("G1", "see this", attachments=[{"type": "image", "url": "https://i.example/x.jpg"}])

    import json
    body = json.loads(http.calls[0]["body"])
    assert body["message"]["attachments"] == [{"type": "image", "url": "https://i.example/x.jpg"}]


# --- edit_message ------------------------------------------------------

def test_edit_message_uses_v4_path_unwrapped_body(http):
    """The lone v4 endpoint in banter's surface. Pin both the path
    base and the unwrapped (no `message` key) body shape — if either
    drifts back to v3 / wrapped, the edit silently 404s or 400s."""
    http.push_json({"meta": {"code": 200}, "response": {"message": {"text": "edited"}}})

    api = GroupMeAPI(token="t")
    api.edit_message("G1", "M1", "edited", attachments=[])

    call = http.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].startswith("https://api.groupme.com/v4/groups/G1/messages/M1")

    import json
    body = json.loads(call["body"])
    assert "message" not in body  # NOT wrapped
    assert body == {"text": "edited", "attachments": []}


def test_edit_message_falls_back_to_dm_path(http):
    """`edit_message` first tries `/groups/<id>/messages/<mid>`; on
    failure it retries `/conversations/<id>/messages/<mid>` so the
    same call works for DMs. The second response is what gets
    returned to the caller."""
    # First (group) attempt fails with 404.
    http.push_http_error(404, {"meta": {"code": 404, "errors": ["not found"]}})
    # Second (conversation) attempt succeeds.
    http.push_json({
        "meta": {"code": 200},
        "response": {"message": {"text": "edited via DM"}},
    })

    api = GroupMeAPI(token="t")
    result = api.edit_message("C1", "M1", "edited via DM")

    assert len(http.calls) == 2
    assert "/groups/C1/" in http.calls[0]["url"]
    assert "/conversations/C1/" in http.calls[1]["url"]
    assert result == {"text": "edited via DM"}
