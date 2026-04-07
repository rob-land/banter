"""Banter — persistent configuration (accounts, preferences, mutes)."""

import json
import time

from .constants import CONFIG_DIR


class Config:
    def __init__(self):
        self._file = CONFIG_DIR / "config.json"
        self._data = self._load()

    def _load(self):
        if self._file.exists():
            try:
                return json.loads(self._file.read_text())
            except Exception:
                pass
        return {"accounts": [], "active_account": None}

    def save(self):
        self._file.write_text(json.dumps(self._data, indent=2))

    # ── accounts ──
    def add_account(self, token: str, user: dict):
        uid = str(user["id"])
        accounts = self._data.setdefault("accounts", [])
        record = {
            "token"       : token,
            "name"        : user.get("name", ""),
            "avatar_url"  : user.get("avatar_url", ""),
            "phone_number": user.get("phone_number", ""),
            "email"       : user.get("email", ""),
            "redirect_url": user.get("redirect_url", ""),
        }
        for acc in accounts:
            if acc["user_id"] == uid:
                acc.update(record)
                self._data["active_account"] = uid
                self.save(); return
        record["user_id"] = uid
        accounts.append(record)
        self._data["active_account"] = uid
        self.save()

    def remove_account(self, user_id: str):
        self._data["accounts"] = [
            a for a in self._data.get("accounts", [])
            if a["user_id"] != str(user_id)
        ]
        if self._data.get("active_account") == str(user_id):
            remaining = self._data["accounts"]
            self._data["active_account"] = (
                remaining[0]["user_id"] if remaining else None
            )
        self.save()

    def get_active_account(self):
        uid = self._data.get("active_account")
        for acc in self._data.get("accounts", []):
            if acc["user_id"] == uid:
                return acc
        return None

    def set_active_account(self, user_id: str):
        self._data["active_account"] = str(user_id)
        self.save()

    def get_accounts(self):
        return list(self._data.get("accounts", []))

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
