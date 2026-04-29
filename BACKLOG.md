# Banter backlog

Things deferred for later. Not a bug tracker — notes to self.

## Feature ideas — not yet started

These came out of the "what's missing vs the official client" survey
and haven't been touched yet. Loose priority order, top = most useful.

- **GIF picker** in the compose bar (Tenor or Giphy). GroupMe accepts
  image attachments by URL, so any web GIF works once we have a
  picker UI. Tenor has a free tier with a Google Cloud key.
- **Pin / unpin messages.** GroupMe data already exposes `pinned_at`
  and `pinned_by`; the official client has the action. Likely an
  undocumented v3 endpoint along the lines of `POST /messages/{cid}/{mid}/pin`
  — capture from web client when ready (same playbook as edit).
- **File (non-image) attachments** — GroupMe supports a `file`
  attachment type but it's gated behind a separate upload flow that's
  not in the public docs. Lower priority — most users send images.
- **"Jump to date" navigator** for long histories. Calendar picker in
  the search bar, fetch by `before_id` until we cross the target
  timestamp, scroll there.
- **Per-conversation mute** with timed options ("1 hour", "8 hours")
  instead of just permanent toggle. The config layer (`set_mute(key, until_epoch)`)
  already supports it; only the UI is missing.
- **Quote-reply tweaks** — currently the reply preview shows
  `"Replying to <name>: <text>"` as plain text; could borrow the
  styled left-bar treatment that incoming reply quotes use.

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
