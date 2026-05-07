# Banter TODO

Things deferred for later. Not a bug tracker — notes to self.

## Feature ideas — not yet started

These came out of the "what's missing vs the official client" survey
and haven't been touched yet. Loose priority order, top = most useful.

- **GIF picker** in the compose bar (Tenor or Giphy). GroupMe accepts
  image attachments by URL, so any web GIF works once we have a
  picker UI. Tenor has a free tier with a Google Cloud key.
- **Forward message** — pick a destination conversation for an
  existing message bubble. Banter-specific feature (the official
  GroupMe clients don't expose forwarding — they lean on copy-paste
  and the OS share sheet instead). Easy for text + image attachments
  (i.groupme.com URLs work cross-conv); harder for files (file_id
  is conv-scoped, would need download + re-upload) and voice notes
  (m.groupme.com URL might not auth in the destination's render
  context). Scope an MVP to text + images and refuse the rest with
  a toast if we revisit.
- **Edit My Profile** UI — `api.update_me` exists; no entry point
  exposes it.
- **Full group administration dialog** — `GroupSettingsDialog`
  covers name/description/mute/delete/leave today, but a richer
  admin surface would include: promote/demote co-owners (using
  `change_owners`), kick members from the settings dialog (today
  it's only on the members list), per-member role display, and
  group-policy toggles (who-can-add-members, posting permission).
  Expand `GroupSettingsDialog` rather than building a parallel
  dialog.
- **Add to album from message bubbles** — Banter's "Add to Album"
  flow lives in the gallery (multi-select). Right-clicking an image
  in a regular chat bubble could surface the same picker for a single
  attachment.
- **Album edit / delete UI** — `PUT /v3/conversations/{cid}/albums/update`
  is captured but no UI calls it. No delete endpoint captured yet.
- **Voice-message send shape** — receive side now lands as an inline
  voice clip (`type:"audio"` attachment via `m.groupme.com`, see
  `VoiceAttachment`). Banter still uploads outgoing recordings as
  generic OGG files via `file.groupme.com`, so they reach official
  clients as a download icon rather than a voice clip. Capture a
  send-side HAR (web client recording + sending a voice message) to
  learn the upload host/path; based on the upload-id format it's
  almost certainly `m.groupme.com` rather than `file.groupme.com`,
  and the encoder needs to produce M4A/AAC + a peaks array.
- **Inline call presence** — `GET /v3/conversations/{cid}/call` returns
  the active meeting URL for a conversation. Polling that (or finding
  the matching push event) would let Banter show a "Call in progress —
  join via browser" banner instead of requiring the user to click
  the call button to discover one.
- **System calendar export** for events (.ics download).
- **Bookmarks / starred messages.**
- **Online / last-seen presence** indicators.
- **Quote-reply tweaks** — the reply preview shows
  `"Replying to <name>: <text>"` as plain text; could borrow the
  styled left-bar treatment that incoming reply quotes use.
- **Full in-app calls** — Audio/video calls are launchable today
  (Start Call → opens Teams in browser) but not embedded. Doing
  embedded calls would mean the Azure Communication Services Web
  SDK (closed-source) or a hand-rolled WebRTC pipeline against the
  same SFU. See GROUPME_API.md "What's deliberately not
  implemented".

## Recently shipped (drop from this list when noticed)

- ~~Pin / unpin messages~~ — `PinnedDialog`, `pin_message`, `unpin_message`
- ~~File (non-image) attachments~~ — `chat_view._pick_attachment`
  routes non-images via `upload_file`
- ~~"Jump to date" navigator~~ — `JumpToDateDialog`
- ~~Per-conversation timed mute~~ — bell-button menu with
  `win.set-mute(int32)` action
- ~~Inline video playback~~ — `VideoAttachment` widget; click-to-play
  with thumbnail preview, right-click to save the original file
- ~~Voice messages~~ (partial) — compose-bar mic button records via
  GStreamer (Opus/Ogg) and uploads through `upload_file`; recipients
  see a generic file rather than an inline voice clip until the
  proper voice-clip endpoint is captured. **Receive side now inline:**
  `type:"audio"` attachments render as a play button + waveform +
  duration via `VoiceAttachment`, with the server's "please update"
  fallback text suppressed.
- ~~Album creation, browsing, multi-select add~~ — `AlbumCreatorDialog`,
  `AlbumViewDialog`, `AlbumPickerDialog`; gallery has a Select
  toggle for multi-pick across both images and videos
- ~~Read receipts~~ — Banter sends a per-conversation `read_receipt`
  on chat open / new message at bottom / scroll-back-to-bottom; own
  DM bubbles show a ✓ Read indicator once the other user's
  `read_receipt.message_id` advances past them; sidebar unread
  badges sync from `unread_count` / `last_read_message_id` and a
  "Mark All Read" item lives in the primary menu. Groups don't
  expose per-member receipts so group bubbles stay unmarked
- ~~Start / Join Call~~ (browser-launch) — call button in chat headers
  hits `GET /v3/conversations/{cid}/call` and opens the returned
  Teams meeting URL via the system browser. **WIP / partially broken:**
  the Teams web meeting drops the visitor in a "someone will let you
  in" lobby that never resolves, and on mobile the Teams web flow
  forwards to the Play Store rather than completing in-browser. Same
  story for the Faye `group.call.started` desktop notification's Join
  button — fires correctly, but the resulting Teams page is unusable.
  The notification itself is still useful (the official GroupMe
  client shows nothing). Real fix likely requires speaking the ACS
  Web Calling SDK protocol natively (Trouter WebSocket + WebRTC +
  Skype conv API) — see "Full in-app calls" below.

## Background-notifier follow-ups

**Status:** core feature works. `banter --background` runs the
`BanterNotifier` daemon (push + REST catch-up + persisted watermark);
the **Notifications → Run in background** toggle in the Accounts
prefs dialog wires up xdg-desktop-portal autostart. Three known gaps:

- **Live-socket transplant** — when a notification click escalates
  background → window, the notifier is `stop()`ped and the new
  `BanterWindow` opens its own Faye WebSocket from scratch. A brief
  reconnect window where push events are missed. Fix: hand off the
  live `GroupMePush` + verified `GroupMeAPI` from the notifier into
  the window so no handshake happens. Needs a constructor branch on
  `BanterWindow` (it currently builds those itself in `__init__`).

- **Window-mode watermark sync** — `BanterWindow._last_msg_ids` lives
  in memory only; only `BanterNotifier` persists to disk via
  `Config.set_last_seen_map`. After a window-only session followed
  by a `--background` launch, the notifier may re-fire notifications
  for messages the user already saw in-app. Fix: have the window
  write its `_last_msg_ids` into `Config` on each push/bg-poll
  update (or at least on shutdown).

- **Portal parent handle** — `background_portal.request_background`
  is called with `parent_xdg_handle=""`, so the portal prompt is
  unparented. Pass the prefs dialog's `Gtk.Native` handle (via
  `xdp_parent_export`) so the prompt attaches to the dialog.

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
