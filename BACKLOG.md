# Banter backlog

Things deferred for later. Not a bug tracker — notes to self.

## Feature ideas — not yet started

These came out of the "what's missing vs the official client" survey
and haven't been touched yet. Loose priority order, top = most useful.

- **GIF picker** in the compose bar (Tenor or Giphy). GroupMe accepts
  image attachments by URL, so any web GIF works once we have a
  picker UI. Tenor has a free tier with a Google Cloud key.
- **Voice messages** — record/send short audio clips. Official
  GroupMe (mobile) has this; no specific API endpoints captured yet.
- **Inline video playback** — Banter currently treats `.mp4` as a
  generic file attachment (downloadable). Official client plays it
  inline. Would need `Gtk.MediaFile` + `Gtk.Video`.
- **Read receipts** — GroupMe shows reader avatars at the bottom of
  recent messages. No `mark_read` / `read_at` plumbing in api.py;
  capture from official client first.
- **Forward message** — pick a destination conversation for an
  existing message bubble. Common GroupMe action.
- **Edit My Profile** UI — `api.update_me` exists; no entry point
  exposes it.
- **Mark all read** / per-conversation mark-read.
- **Album creation** — `api.create_album`, `add_photo_to_album`, and
  `get_album_photos` are already implemented; no UI calls them. Either
  build the UI or delete the unused API methods.
- **System calendar export** for events (.ics download).
- **Bookmarks / starred messages.**
- **Online / last-seen presence** indicators.
- **Quote-reply tweaks** — the reply preview shows
  `"Replying to <name>: <text>"` as plain text; could borrow the
  styled left-bar treatment that incoming reply quotes use.
- **Audio / video calls.** GroupMe groups support multi-party
  calls in the official clients; Banter is silent on them. Big
  effort — see GROUPME_API.md "What's deliberately not
  implemented" for the technical scope. A reasonable interim
  feature would be passive call detection: when a group call
  starts, surface a banner / notification that says "Call in
  progress — open in web/mobile to join." Capture a HAR while
  initiating a call from web.groupme.com to find the signaling
  endpoint.

## Recently shipped (drop from this list when noticed)

- ~~Pin / unpin messages~~ — `PinnedDialog`, `pin_message`, `unpin_message`
- ~~File (non-image) attachments~~ — `chat_view._pick_attachment`
  routes non-images via `upload_file`
- ~~"Jump to date" navigator~~ — `JumpToDateDialog`
- ~~Per-conversation timed mute~~ — bell-button menu with
  `win.set-mute(int32)` action

## Edit-message UX polish

**Status:** core feature works. Two small issues for later.

- Error message when an edit fails after the server-side time window
  is just "Failed to edit message". Worth surfacing the actual
  reason ("GroupMe edit window has expired") if the response gives us
  one — would need to pass through the `meta.errors` payload.
- DM edits go untested. `api.edit_message` falls back to
  `PUT /v4/conversations/{cid}/messages/{mid}` after the group path,
  but no DM HAR has been captured to verify shape.

---

## Pack picker doesn't match the official GroupMe client

**Status:** deferred, cosmetic.

Banter fetches `GET https://powerup.groupme.com/powerups` and shows every pack it returns. That endpoint returns the *full historical catalog* — including retired packs like the South Park set. The official GroupMe clients show a different (smaller) pack list, so the two clients' pickers don't match. Reactions cross-render fine in both directions (because the server accepts any valid pack_id), just the available choices differ.

**To fix:** inspect the official client's network traffic. Likely candidates:
- `GET /users/me/powerups` — user-activated packs only
- `GET /powerups?default=true` or similar filter on the same endpoint
- A completely different host (e.g. behind the auth-gated API rather than `powerup.groupme.com`)

Once we know the right endpoint, swap `api.get_powerups()` over.

## Compose bar covered by OSK on Phosh

**Status:** deferred.

On the OnePlus 6T (and presumably other Phosh-based ROMs), when the on-screen keyboard pops up, it covers the compose bar rather than the window resizing to make room.

**What was tried:**
- Restructured `ChatView` to use `Adw.ToolbarView` with the compose bar as an `add_bottom_bar()` — didn't help.
- Dropped the window's `set_size_request` min height from 600 → 360 so the compositor could shrink the window — didn't help.

**Likely cause:**
Phoc (the Phosh compositor) may not emit `xdg_toplevel.configure` events to reduce the window geometry when squeekboard appears on this particular ROM. The layer-shell-based OSK sits on top of the window without informing it. Fractal works on newer Phosh builds — the fix may simply be a ROM/compositor update away, or it may require manual `zwp_text_input_v3` handling.

**Ideas to try:**
- Listen for focus events on the compose `Gtk.TextView` and manually set `margin-bottom` on the window content.
- Inspect Fractal's source (`src/session/view/room/mod.rs`?) for any explicit OSK handling code.
- Check if `squeekboard` version on the 6T supports layer-shell geometry hints.
- Try `Gtk.Settings:gtk-enable-accels` / IM-module settings.
