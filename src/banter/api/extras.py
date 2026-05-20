"""GroupMe API — contacts, blocks, events, polls, calls, powerups, search.

Everything that doesn't fit conversations / DMs / media. Mixed in
last so the more-common features take precedence in MRO when names
happen to overlap (none currently, but a stable convention).
"""

from ..constants import GROUPME_POWERUPS


class ExtrasMixin:
    # ── contacts ──
    def get_contacts(self):
        r = self._req("GET", "/contacts")
        return r.get("response", [])

    # ── blocks ──
    def block_user(self, me_id, other_user_id):
        """Block another user. GroupMe's /blocks endpoint takes the
        users as query parameters rather than JSON body."""
        r = self._req("POST", "/blocks",
                      params={"user":      str(me_id),
                              "otherUser": str(other_user_id)})
        return self._ok(r)

    def unblock_user(self, me_id, other_user_id):
        r = self._req("DELETE", "/blocks",
                      params={"user":      str(me_id),
                              "otherUser": str(other_user_id)})
        return self._ok(r)

    # ── Calls (Microsoft Teams meetings under the hood) ──
    # GroupMe calls aren't a Banter-implementable WebRTC feature —
    # the server returns a Teams meeting URL and an Azure
    # Communication Services token, and the official client embeds
    # the Teams call composite. From a Linux client without that
    # SDK, the best we can do is open the meeting URL in the user's
    # browser, which has working camera/mic permissions and the
    # full Teams UI.

    def get_call(self, gid):
        """Get (or lazily create) the call session for a conversation.
        Returns ``{token, expires_on, meeting_type, meeting_id}`` —
        ``meeting_id`` is a teams.live.com URL the user can join in
        any browser. Returns None if no call is reachable."""
        r = self._req("GET", f"/conversations/{gid}/call")
        if not self._ok(r):
            return None
        return r.get("response")

    # ── calendar events ──
    def get_events(self, gid):
        r = self._req("GET", f"/conversations/{gid}/events/list")
        return r.get("response", {}).get("events", [])

    def create_event(self, gid, name: str, start_at: str,
                     end_at: str = None, location: str = "",
                     all_day: bool = False, tz: str = "UTC"):
        """Create a calendar event. GroupMe requires start_at / end_at /
        timezone / is_all_day be provided together — end_at defaults to
        start_at (zero-duration) if the caller didn't supply one."""
        payload = {
            "name"       : name,
            "start_at"   : start_at,
            "end_at"     : end_at or start_at,
            "timezone"   : tz,
            "is_all_day" : bool(all_day),
        }
        if location:
            payload["location"] = {"name": location}
        return self._req("POST",
                         f"/conversations/{gid}/events/create", payload)

    def rsvp_event(self, gid, event_id, status):
        """RSVP to an event. status: 'going' or 'not_going'."""
        r = self._req("POST",
                      f"/conversations/{gid}/events/{event_id}/rsvps",
                      {"rsvp": {"status": status}})
        return self._ok(r)

    def get_event(self, gid, event_id):
        """Return a single event dict by id, or None if not found."""
        events = self.get_events(gid) or []
        for e in events:
            if str(e.get("event_id") or e.get("id") or "") == str(event_id):
                return e
        return None

    def get_event_rsvps(self, gid, event_id):
        r = self._req("GET",
                      f"/conversations/{gid}/events/{event_id}/rsvps")
        return r.get("response", {})

    # ── Powerups (GroupMe's proprietary emoji packs) ──
    def get_powerups(self):
        """Fetch the full GroupMe emoji-pack catalog.

        The endpoint has returned multiple shapes in the wild:
          • flat list  ................... [ {...}, {...} ]
          • flat dict  ................... {"powerups": [...]}
          • wrapped dict  ................ {"meta": ..., "response": {"powerups": [...]}}
          • wrapped list inside response . {"meta": ..., "response": [...]}
        Handle all of them, return a list of pack dicts or []."""
        r = self._req("GET", "/powerups", base=GROUPME_POWERUPS)
        if r is None:
            return []
        if isinstance(r, list):
            return r
        if isinstance(r, dict):
            if isinstance(r.get("powerups"), list):
                return r["powerups"]
            resp = r.get("response")
            if isinstance(resp, list):
                return resp
            if isinstance(resp, dict) and isinstance(resp.get("powerups"), list):
                return resp["powerups"]
        return []

    # ── polls ──
    # The /poll/{gid} list returns each poll wrapped as
    # `{"data": {…fields…}, …}` (push events use the same wrapper).
    # `_unwrap_poll` flattens that so callers always see the field
    # block. Field shape (recovered from push capture):
    #   subject, options[].id/title/votes (votes absent when 0),
    #   type ("single"|"multi"), status ("active"|"ended"|"deleted"),
    #   visibility ("anonymous"|"public"), expiration (unix seconds).

    @staticmethod
    def _unwrap_poll(p: dict) -> dict:
        if isinstance(p, dict) and isinstance(p.get("data"), dict) \
                and "subject" in p["data"]:
            return p["data"]
        return p or {}

    def get_polls(self, gid):
        r = self._req("GET", f"/poll/{gid}")
        return [self._unwrap_poll(p)
                for p in r.get("response", {}).get("polls", [])]

    def get_poll(self, gid, poll_id):
        target = str(poll_id)
        for p in self.get_polls(gid):
            if str(p.get("id", "")) == target:
                return p
        return None

    def create_poll(self, gid, subject: str, options: list,
                    expiry: int = 86400, multichoice: bool = False):
        r = self._req("POST", f"/poll/{gid}", {
            "subject"     : subject,
            "options"     : [{"title": o} for o in options],
            "expiration"  : expiry,
            "type"        : "multi_choice" if multichoice else "single_choice",
            "visibility"  : "public",
        })
        return r.get("response")

    def vote_poll(self, gid, poll_id: str, option_ids: list):
        r = self._req("POST", f"/poll/{gid}/{poll_id}/votes",
                      {"options": option_ids})
        return self._ok(r)

    # ── member search (for add-member) ──
    def search_users(self, query: str):
        r = self._req("GET", "/users/search",
                      params={"query": query})
        return r.get("response", [])
