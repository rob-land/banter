"""Banter — GroupMe REST API client.

The original `api.py` packed every endpoint into a single 1054-line
class. It's now split into feature mixins under `api/`; this module
assembles them into the public `GroupMeAPI` class so external
imports (`from banter.api import GroupMeAPI`) stay unchanged.

Layout:
  _core   — base: __init__, _req, _ok, connectivity hooks, auth/me
  groups  — groups, members, messages, pins, reactions
  dms     — DM conversations + read receipts
  media   — gallery, albums, image / file / audio upload + download
  extras  — contacts, blocks, events, polls, calls, powerups, search
"""

from ._core import _APIBase
from .dms import DMsMixin
from .extras import ExtrasMixin
from .groups import GroupsMixin
from .media import MediaMixin


class GroupMeAPI(
    GroupsMixin,
    DMsMixin,
    MediaMixin,
    ExtrasMixin,
    _APIBase,
):
    """Public client. All endpoints live on the mixins; this class is
    the assembly point and the only thing the rest of Banter imports."""


__all__ = ["GroupMeAPI"]
