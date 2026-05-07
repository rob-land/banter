"""Banter — persistent configuration (accounts, preferences, mutes)."""

import json
import os
import time

from .constants import CONFIG_DIR, dbg
from . import secrets


class Config:
    def __init__(self):
        self._file = CONFIG_DIR / "config.json"
        self._data = self._load()
        # One-shot migration: pre-libsecret installs persisted the token
        # in the same JSON blob as the rest of the account record. Move
        # any plaintext tokens we find into the keyring and strip them
        # from the on-disk record so a future leak of config.json
        # doesn't expose them. No-op once everyone's migrated.
        self._migrate_plaintext_tokens()

    def _load(self):
        if self._file.exists():
            # One-time correction: existing installs may have a 0644
            # config.json from before this hardening. Tighten on every
            # load so users get the protection without a manual fix.
            try:
                os.chmod(self._file, 0o600)
            except OSError:
                pass
            try:
                return json.loads(self._file.read_text())
            except Exception:
                pass
        return {"accounts": [], "active_account": None}

    def save(self):
        """Atomically persist to disk with 0600 permissions.

        Write to a sibling temp file then rename over the real one, so
        a crash / SIGKILL between open and close can't leave a
        half-written config.json and lose the user's accounts.
        os.replace is atomic on POSIX.

        chmod is set on the temp file BEFORE the rename so there's no
        moment when the final file is world-readable. Defense in depth
        — even after libsecret moves the token out of this file, leaks
        of the remaining metadata (account names, mute lists) aren't
        helpful to an attacker."""
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass   # best-effort; tmpfs / odd FS may reject
        os.replace(tmp, self._file)

    # ── accounts ──
    #
    # Account records on disk hold non-secret metadata only — name,
    # avatar URL, etc. The token lives in the keyring (libsecret) and
    # is fetched lazily on read. add_account / remove_account keep the
    # two stores in sync; get_active_account / get_accounts attach the
    # token to the returned dict so callers don't have to know.
    def _migrate_plaintext_tokens(self):
        accounts = self._data.get("accounts") or []
        dirty = False
        for acc in accounts:
            tok = acc.get("token")
            if not tok:
                continue
            uid = str(acc.get("user_id", ""))
            ok = secrets.store_token(uid, acc.get("name", ""), tok) if uid else False
            if ok:
                acc.pop("token", None)
                dirty = True
                dbg("config: migrated plaintext token for %s to keyring", uid)
            else:
                # Keyring unreachable — leave the plaintext token where
                # it is so the user can still sign in. The save() call
                # below tightens the file mode to 0600 either way.
                dbg("config: keyring unavailable, keeping plaintext token for %s", uid)
        if dirty:
            self.save()

    def _attach_token(self, acc):
        """Return a copy of `acc` with `token` populated from the keyring
        (or, as a fallback, the legacy on-disk plaintext field). Returns
        None on input None."""
        if acc is None:
            return None
        out = dict(acc)
        uid = str(acc.get("user_id", ""))
        keyring_tok = secrets.lookup_token(uid) if uid else None
        if keyring_tok:
            out["token"] = keyring_tok
        # else: leave whatever's already in `acc` (legacy plaintext, or
        # missing — caller has to handle)
        return out

    def add_account(self, token: str, user: dict):
        uid = str(user["id"])
        accounts = self._data.setdefault("accounts", [])
        record = {
            "name"        : user.get("name", ""),
            "avatar_url"  : user.get("avatar_url", ""),
            "phone_number": user.get("phone_number", ""),
            "email"       : user.get("email", ""),
            "redirect_url": user.get("redirect_url", ""),
        }
        # Try the keyring first; on failure, fall back to plaintext on
        # disk so the user can still sign in. The latter is the same
        # protection level we had before — defense in depth, not a
        # regression.
        if not secrets.store_token(uid, record["name"], token):
            record["token"] = token

        for acc in accounts:
            if acc["user_id"] == uid:
                # Re-adding an existing account: reset the record so
                # leftover legacy plaintext tokens get removed.
                acc.clear()
                acc.update(record)
                acc["user_id"] = uid
                self._data["active_account"] = uid
                self.save(); return
        record["user_id"] = uid
        accounts.append(record)
        self._data["active_account"] = uid
        self.save()

    def remove_account(self, user_id: str):
        uid = str(user_id)
        secrets.clear_token(uid)   # idempotent if not present
        self._data["accounts"] = [
            a for a in self._data.get("accounts", [])
            if a["user_id"] != uid
        ]
        if self._data.get("active_account") == uid:
            remaining = self._data["accounts"]
            self._data["active_account"] = (
                remaining[0]["user_id"] if remaining else None
            )
        self.save()

    def get_active_account(self):
        uid = self._data.get("active_account")
        for acc in self._data.get("accounts", []):
            if acc["user_id"] == uid:
                return self._attach_token(acc)
        return None

    def set_active_account(self, user_id: str):
        self._data["active_account"] = str(user_id)
        self.save()

    def get_accounts(self):
        return [self._attach_token(a)
                for a in self._data.get("accounts", [])]

    # ── preferences ──
    def get_pref(self, key: str, default=None):
        return self._data.get("prefs", {}).get(key, default)

    def set_pref(self, key: str, value):
        self._data.setdefault("prefs", {})[key] = value
        self.save()

    # ── per-group mute  (value = epoch to unmute, or -1 = permanent) ──
    def get_mute(self, group_id: str):
        return self._data.get("mutes", {}).get(str(group_id))

    def set_mute(self, group_id: str, until):
        self._data.setdefault("mutes", {})[str(group_id)] = until
        self.save()

    def clear_mute(self, group_id: str):
        self._data.get("mutes", {}).pop(str(group_id), None)
        self.save()

    def is_muted(self, group_id: str) -> bool:
        v = self.get_mute(group_id)
        if v is None:
            return False
        if v == -1:
            return True
        return time.time() < v

    # ── per-account last-seen message ids (background notifier) ──
    #
    # Keyed by user_id so multi-account installs don't collide. The inner
    # map is conv_key → message_id, where conv_key is the same string
    # form the notifier uses internally ("group:<gid>" / "dm:<other_id>").
    # Window mode doesn't read this — it seeds its in-memory map from
    # the current /groups+/chats responses on startup. The notifier is
    # the only consumer; persistence lets it tell "new since last
    # background run" from "ancient backlog" across reboots.
    def get_last_seen_map(self, user_id: str) -> dict:
        return dict(self._data.get("last_seen", {}).get(str(user_id), {}))

    def set_last_seen_map(self, user_id: str, mapping: dict):
        self._data.setdefault("last_seen", {})[str(user_id)] = dict(mapping)
        self.save()
