# Banter backlog

Things deferred for later. Not a bug tracker — notes to self.

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
