
import logging

log = logging.getLogger(__name__)
"""Banter — libsecret wrapper for OAuth tokens.

Stores GroupMe tokens in the desktop keyring (gnome-keyring,
KWallet via the org.freedesktop.secrets D-Bus interface, etc.)
instead of the plaintext config file. Each account's token is keyed
by its `user_id` so multi-account sessions don't collide.

If libsecret isn't available — running outside a desktop session,
broken keyring daemon, missing D-Bus access in a sandboxed runtime
— `lookup` / `store` / `clear` return None / False instead of
raising, so the caller can fall back to the legacy plaintext field.
"""


try:
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    _AVAILABLE = True
except (ValueError, ImportError) as e:
    log.debug("secrets: libsecret unavailable (%s) — falling back to plaintext", e)
    Secret = None
    _AVAILABLE = False


_SCHEMA = None


def _schema():
    """Lazy-build the Secret.Schema. Done lazily so module import doesn't
    fail on systems where libsecret's typelib loaded but the daemon is
    broken."""
    global _SCHEMA
    if _SCHEMA is not None or not _AVAILABLE:
        return _SCHEMA
    _SCHEMA = Secret.Schema.new(
        "land.rob.banter.Token",
        Secret.SchemaFlags.NONE,
        {"user_id": Secret.SchemaAttributeType.STRING},
    )
    return _SCHEMA


def is_available() -> bool:
    """True if libsecret is importable. Doesn't probe the daemon — that
    only happens on a real call."""
    return _AVAILABLE


def store_token(user_id: str, name: str, token: str) -> bool:
    """Persist `token` under `user_id` in the system keyring. Returns
    True on success."""
    if not _AVAILABLE:
        return False
    try:
        ok = Secret.password_store_sync(
            _schema(),
            {"user_id": str(user_id)},
            Secret.COLLECTION_DEFAULT,
            f"Banter token for {name or user_id}",
            token,
            None,
        )
        log.debug("secrets: stored token for %s → %s", user_id, ok)
        return bool(ok)
    except Exception as e:
        log.debug("secrets: store failed for %s: %s", user_id, e)
        return False


def lookup_token(user_id: str):
    """Return the stored token for `user_id`, or None if not present /
    keyring unreachable."""
    if not _AVAILABLE:
        return None
    try:
        val = Secret.password_lookup_sync(
            _schema(), {"user_id": str(user_id)}, None,
        )
        return val
    except Exception as e:
        log.debug("secrets: lookup failed for %s: %s", user_id, e)
        return None


def clear_token(user_id: str) -> bool:
    """Remove the stored token for `user_id`. Returns True on success
    (including the not-found case — idempotent removal)."""
    if not _AVAILABLE:
        return False
    try:
        Secret.password_clear_sync(
            _schema(), {"user_id": str(user_id)}, None,
        )
        return True
    except Exception as e:
        log.debug("secrets: clear failed for %s: %s", user_id, e)
        return False
