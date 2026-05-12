"""Banter — mock GroupMe API for `--demo` screenshot mode.

When the app starts with `--demo`, the GroupMeAPI instance is replaced
with MockGroupMeAPI. It loads canned data from `banter-demo.json`
(installed alongside the app's data files) and serves it through the
same method surface the real client exposes. Mutations update the
in-memory state so the UI behaves naturally during a screenshot
session — sent messages appear, reactions toggle, edits stick.

Methods that aren't explicitly implemented fall through __getattr__ to
a no-op stub that returns None or {}. That covers the long tail
(events, polls, member ops, file uploads) without forcing every demo
session to also exercise them.
"""

import json
import os
import time
from copy import deepcopy
from pathlib import Path

from .constants import APP_NAME

import logging

log = logging.getLogger(__name__)


def _candidate_paths():
    """Where to look for banter-demo.json, in priority order."""
    # Installed data dir (Flatpak, system install, staging root)
    for d in os.environ.get("XDG_DATA_DIRS", "/usr/share").split(":"):
        if d:
            yield Path(d) / "banter" / "banter-demo.json"
    # Source-tree fallback for `meson install --destdir` runs that
    # haven't propagated to XDG_DATA_DIRS, and for direct dev runs.
    yield Path(__file__).resolve().parent.parent / "data" / "banter-demo.json"


def _load_demo_data():
    for p in _candidate_paths():
        if p.exists():
            log.debug("mock: loading demo data from %s", p)
            with p.open() as f:
                return json.load(f)
    raise FileNotFoundError(
        f"{APP_NAME}: banter-demo.json not found in XDG_DATA_DIRS "
        f"or source tree — install the app or run from the repo root.")


class MockGroupMeAPI:
    """Drop-in replacement for GroupMeAPI in --demo mode."""

    def __init__(self, token: str = None, on_unauthorized=None,
                 on_online=None, on_offline=None):
        self.token = token or "demo-token"
        self._data = _load_demo_data()
        # Counter for generated message ids, seeded from current time so
        # ids are monotonic across restarts within a session.
        self._next_id = int(time.time() * 1000)

    # ── Helpers ──────────────────────────────────────────────────────
    def _gen_id(self) -> str:
        self._next_id += 1
        return f"local-{self._next_id}"

    def _me(self) -> dict:
        return self._data["user"]

    def _group(self, gid):
        gid = str(gid)
        for g in self._data["groups"]:
            if str(g["id"]) == gid:
                return g
        return None

    def _dm(self, other_user_id):
        oid = str(other_user_id)
        for c in self._data["dms"]:
            if str(c["other_user"]["id"]) == oid:
                return c
        return None

    def _new_message(self, text: str, attachments=None) -> dict:
        me = self._me()
        return {
            "id":             self._gen_id(),
            "source_guid":    self._gen_id(),
            "user_id":        me["id"],
            "name":           me["name"],
            "avatar_url":     me.get("avatar_url"),
            "text":           text,
            "created_at":     int(time.time()),
            "favorited_by":   [],
            "attachments":    list(attachments or []),
            "system":         False,
        }

    # ── Auth / identity ──────────────────────────────────────────────
    def verify_token(self, token: str):
        return self._me()

    def get_me(self):
        return self._me()

    # ── Groups ───────────────────────────────────────────────────────
    def get_groups_all(self):
        # Strip our internal `_messages` key from the public payload to
        # match the real API response shape.
        return [{k: v for k, v in g.items() if k != "_messages"}
                for g in self._data["groups"]]

    def get_groups_all_with_members(self):
        return self.get_groups_all()

    def get_group(self, gid):
        g = self._group(gid)
        if g is None:
            return None
        return {k: v for k, v in g.items() if k != "_messages"}

    def get_members(self, gid):
        g = self._group(gid)
        return list(g.get("members", [])) if g else []

    def create_group(self, name: str, description: str = "",
                     share: bool = True):
        gid = str(self._next_id); self._next_id += 1
        me  = self._me()
        new = {
            "id": gid, "name": name, "description": description,
            "image_url": None,
            "share_url": f"https://groupme.test/join_group/{gid}/x" if share else None,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "members": [{"id": me["id"], "user_id": me["id"],
                         "nickname": me["name"], "image_url": None}],
            "messages": {"count": 0, "last_message_id": None,
                          "last_message_created_at": None,
                          "preview": {"nickname": "", "text": "",
                                       "image_url": None, "attachments": []}},
            "_messages": [],
        }
        self._data["groups"].append(new)
        return {k: v for k, v in new.items() if k != "_messages"}

    def update_group(self, gid, **kwargs):
        g = self._group(gid)
        if g is None: return None
        for k, v in kwargs.items():
            if v is not None:
                g[k] = v
        return {k: v for k, v in g.items() if k != "_messages"}

    def destroy_group(self, gid):
        g = self._group(gid)
        if g is not None:
            self._data["groups"].remove(g)
            return True
        return False

    # ── Group messages ───────────────────────────────────────────────
    def get_messages(self, gid, before_id=None, since_id=None,
                     after_id=None, limit=20):
        g = self._group(gid)
        if g is None: return []
        msgs = list(g.get("_messages", []))
        # Sort newest-first to match the real API
        msgs.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        if before_id:
            for i, m in enumerate(msgs):
                if m["id"] == before_id:
                    msgs = msgs[i+1:]
                    break
        # `since_id` and `after_id` are both "newer than this id" filters
        # for our purposes. ChatView polling passes one or the other.
        # If the pivot id doesn't match any known message, return [] —
        # otherwise the unbounded loop would yield every message and
        # _append_new would re-append them as duplicates.
        pivot = since_id or after_id
        if pivot:
            found = False
            cut = []
            for m in msgs:
                if m["id"] == pivot:
                    found = True
                    break
                cut.append(m)
            msgs = cut if found else []
        return msgs[:limit]

    def send_message(self, gid, text: str, attachments=None,
                     source_guid: str = None):
        g = self._group(gid)
        if g is None: return None
        msg = self._new_message(text, attachments)
        if source_guid:
            msg["source_guid"] = source_guid
        g.setdefault("_messages", []).append(msg)
        g["messages"]["last_message_id"] = msg["id"]
        g["messages"]["last_message_created_at"] = msg["created_at"]
        g["messages"]["preview"] = {
            "nickname": msg["name"],
            "text": msg["text"],
            "image_url": None,
            "attachments": msg["attachments"],
        }
        g["messages"]["count"] = g["messages"].get("count", 0) + 1
        g["updated_at"] = msg["created_at"]
        return deepcopy(msg)

    def edit_message(self, conv_id, msg_id, text: str, attachments=None):
        # Could be either a group or DM id — try both.
        for store in self._iter_message_stores():
            for m in store:
                if m["id"] == msg_id:
                    m["text"] = text
                    if attachments is not None:
                        m["attachments"] = list(attachments)
                    return deepcopy(m)
        return None

    def delete_message(self, conv_id, msg_id):
        for store in self._iter_message_stores():
            for m in list(store):
                if m["id"] == msg_id:
                    store.remove(m)
                    return True
        return False

    def _iter_message_stores(self):
        for g in self._data["groups"]:
            yield g.setdefault("_messages", [])
        for c in self._data["dms"]:
            yield c.setdefault("_messages", [])

    # ── Polls ────────────────────────────────────────────────────────
    def get_polls(self, gid):
        g = self._group(gid)
        return list(g.get("_polls", [])) if g else []

    def get_poll(self, gid, poll_id):
        target = str(poll_id)
        for p in self.get_polls(gid):
            if str(p.get("id", "")) == target:
                return p
        return None

    def vote_poll(self, gid, poll_id: str, option_ids: list):
        g = self._group(gid)
        if g is None: return False
        target = str(poll_id)
        for p in g.get("_polls", []):
            if str(p.get("id", "")) != target:
                continue
            chosen = {str(x) for x in option_ids}
            for opt in p.get("options", []):
                oid = str(opt.get("id", ""))
                # In demo mode each click is a fresh +1 — we have no
                # per-voter ledger to detect a re-vote.
                if oid in chosen:
                    opt["votes"] = int(opt.get("votes") or 0) + 1
            p["last_modified"] = int(time.time())
            return True
        return False

    # ── Pinned ───────────────────────────────────────────────────────
    def get_pinned_group(self, gid):
        return []

    def get_pinned_dm(self, other_user_id):
        return []

    # ── Reactions ────────────────────────────────────────────────────
    def like_message(self, gid, msg_id):
        return self._toggle_like(msg_id, add=True)

    def react_message(self, gid, msg_id, code: str):
        return self._toggle_like(msg_id, add=True)

    def react_message_pack(self, gid, msg_id, pack_id, pack_index):
        return self._toggle_like(msg_id, add=True)

    def unreact_message(self, gid, msg_id):
        return self._toggle_like(msg_id, add=False)

    def _toggle_like(self, msg_id, *, add: bool) -> bool:
        me_id = self._me()["id"]
        for store in self._iter_message_stores():
            for m in store:
                if m["id"] == msg_id:
                    favs = m.setdefault("favorited_by", [])
                    if add and me_id not in favs:
                        favs.append(me_id)
                    elif not add and me_id in favs:
                        favs.remove(me_id)
                    return True
        return False

    # ── DMs ──────────────────────────────────────────────────────────
    def get_chats_all(self):
        return [{k: v for k, v in c.items() if k != "_messages"}
                for c in self._data["dms"]]

    def get_dm_messages(self, other_user_id, before_id=None,
                        since_id=None, after_id=None, limit=20):
        c = self._dm(other_user_id)
        if c is None: return []
        msgs = list(c.get("_messages", []))
        msgs.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        if before_id:
            for i, m in enumerate(msgs):
                if m["id"] == before_id:
                    msgs = msgs[i+1:]
                    break
        # Honor `since_id` / `after_id` so the 15-second DM poll loop
        # in ChatView stops re-appending the entire conversation each
        # tick. See get_messages above for the same logic.
        pivot = since_id or after_id
        if pivot:
            found = False
            cut = []
            for m in msgs:
                if m["id"] == pivot:
                    found = True
                    break
                cut.append(m)
            msgs = cut if found else []
        return msgs[:limit]

    def send_dm(self, recipient_id: str, text: str, attachments=None,
                source_guid: str = None):
        c = self._dm(recipient_id)
        if c is None: return None
        msg = self._new_message(text, attachments)
        if source_guid:
            msg["source_guid"] = source_guid
        msg["recipient_id"] = recipient_id
        c.setdefault("_messages", []).append(msg)
        c["last_message"] = {"id": msg["id"], "text": msg["text"],
                              "user_id": msg["user_id"],
                              "created_at": msg["created_at"]}
        c["updated_at"] = msg["created_at"]
        return deepcopy(msg)

    # ── Misc / unused-but-called read paths ──────────────────────────
    def get_gallery(self, gid, before: str = None, after: str = None,
                    limit: int = 20):
        # No image attachments in the demo data; return empty.
        return []

    def get_events(self, gid):
        return []

    def search_users(self, query: str):
        return []

    # ── Catch-all for the long tail (mute, block, file upload, …) ────
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        log.debug("mock: stub %s called", name)
        def _stub(*a, **kw):
            return None
        return _stub
