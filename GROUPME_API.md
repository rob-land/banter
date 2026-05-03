# GroupMe API — what Banter knows

A consolidated reference for the parts of GroupMe's API that Banter
talks to. Mixes the official documentation with reverse-engineered
endpoints captured from `web.groupme.com` network traces. Every
non-obvious detail here is the result of either a HAR capture or a
debugging session — keep that in mind before "cleaning up" anything
that looks weird.

## Hosts

| Host | Purpose |
|---|---|
| `https://api.groupme.com/v3` | Main REST API. Default `base` in `api.py`. |
| `https://api.groupme.com/v4` | One-off newer prefix used only by message edit. |
| `https://image.groupme.com` | Image upload service (different host, different content type). |
| `https://push.groupme.com/faye` | Bayeux/Faye WebSocket push channel. |
| `https://powerup.groupme.com` | Emoji-pack ("powerup") catalog. |
| `https://oauth.groupme.com/oauth/authorize` | OAuth start URL. |

## Authentication

Two equivalent ways to authenticate against the v3 / v4 REST API:

* `?token=<access_token>` query parameter, OR
* `X-Access-Token: <access_token>` header.

Banter sends both unconditionally — the query string from the URL
builder, the header from `_req`. The token is also passed inside Faye
publish frames in `ext.access_token`.

### Mandatory request header

```
X-Requested-With: GroupMeWeb/1.2.3
```

The reactions endpoint **gates non-heart unicode reactions** on this
header — without it the server returns 500 / "internal error" for any
emoji other than ❤️. Banter sends this header on every request to
match the web client. If reactions break again in the future, bump the
version to whatever GroupMe's web bundle currently advertises.

### OAuth

* Authorize URL: `https://oauth.groupme.com/oauth/authorize`
* Callback URL: `http://localhost:7654` (registered with the app)
* Banter's `BANTER_CLIENT_ID` lives in `src/oauth.py`.
* GroupMe redirects via a `banter://` custom URI scheme handled in
  `application.py`.

## Response envelope

Almost all v3/v4 endpoints wrap their payload:

```json
{ "meta": { "code": 200 }, "response": { ... } }
```

`api._ok(r)` checks `meta.code in (200, 201, 204)`. On HTTP errors the
server usually still returns the same envelope with `meta.errors`
populated. 204 No Content responses come back with empty body — `_req`
synthesizes `{"meta":{"code":204},"response":null}` so callers can use
the same shape.

## JSON body encoding

`api._req` posts with:

```python
json.dumps(data, ensure_ascii=False).encode("utf-8")
```

`ensure_ascii=False` matters: the default escapes non-ASCII chars as
`\uXXXX` surrogate pairs, which GroupMe historically rejects on emoji
reactions and likely elsewhere. Send raw UTF-8 bytes to match the web
client.

---

## Endpoints in use

Below: every endpoint Banter currently calls, organised by feature.
Method, path, body shape, and where each lives in `api.py`.

### Auth and user

| | |
|---|---|
| `GET /users/me` | Verify token / fetch own user. `verify_token`, `get_me`. |
| `POST /users/update` | Update your own profile. Body: `{name?, avatar_url?, ...}`. |

### Groups

| | |
|---|---|
| `GET /groups?page=N&per_page=20&omit=memberships` | List joined groups. `omit=memberships` skips the member roster — much faster. Banter uses this for the sidebar. |
| `GET /groups?omit=` (empty) | Same but **with** members — slow, used once on startup to populate the contacts tab. |
| `GET /groups/{gid}` | Single group with full data. |
| `GET /groups/{gid}/members` | Member roster only. |
| `POST /groups` | Create group. Body: `{name, description?, share}`. |
| `POST /groups/{gid}/update` | Update group. Body: any subset of `name`, `description`, `image_url`, `office_mode`. |
| `POST /groups/{gid}/destroy` | Delete a group. |
| `POST /groups/{gid}/join/{share_token}` | Join a group via share token. |
| `POST /groups/{gid}/rejoin` | Rejoin a group you've left. |
| `POST /groups/change_owners` | Body: `{requests: [{group_id, owner_id}]}`. |
| `POST /groups/{gid}/members/add` | Body: `{members: [{user_id, nickname}]}`. |
| `POST /groups/{gid}/members/{membership_id}/remove` | Note: the path uses **membership_id**, not user_id. They're different. |
| `POST /groups/{gid}/memberships/update` | Update your own nickname / membership prefs in a group. |

### Group messages

| | |
|---|---|
| `GET /groups/{gid}/messages?limit=N&before_id=&since_id=&after_id=` | Paginate messages. Newest-first ordering. |
| `POST /groups/{gid}/messages` | Send. **Body must be wrapped:** `{"message": {source_guid, text, attachments?}}`. `source_guid` is a client-generated ms-timestamp dedup key. |
| `PUT /v4/groups/{gid}/messages/{mid}` | **Edit** — see "Editing messages" below. |
| `DELETE /v3/conversations/{cid}/messages/{mid}` | **Delete.** Note the `/conversations/` path — for groups `cid == gid`; for DMs `cid == "<lo>+<hi>"`. |

### Editing messages (undocumented, v4)

Recovered from `web.groupme.com` network traces. Two specific gotchas:

* **`/v4/`, not `/v3/`.** Every other endpoint is v3. Edit alone lives
  on the newer prefix.
* **Body is unwrapped — flat `{text, attachments}`** — *not*
  `{"message": {text, ...}}` like the send endpoint uses. Sending the
  wrapped shape returns 500.

```
PUT https://api.groupme.com/v4/groups/{gid}/messages/{mid}
Content-Type: application/json
X-Access-Token: <token>
X-Requested-With: GroupMeWeb/1.2.3

{"text": "new content", "attachments": []}
```

Server response shape:

```json
{"meta":{"code":200},
 "response":{"message":{"id":"...","text":"new content",
                        "updated_at":<unix>,"created_at":<unix>, ...}}}
```

`updated_at != created_at` ( + 1 s of jitter) is the signal a message
has been edited. Used by `MessageBubble._is_edited()`.

**Time window:** GroupMe enforces a server-side ~10–15 minute window
after `created_at` past which edit returns an error. Banter caps at 10
min via `MessageBubble.EDIT_WINDOW_SECS` to stay conservatively inside
it.

**DM equivalent (untested):** `api.edit_message` falls back to
`PUT /v4/conversations/{cid}/messages/{mid}` with the same flat body.
No DM HAR has confirmed this — capture before relying on it.

Failed paths (kept here so we don't try them again):

* `PUT /v3/conversations/{cid}/messages/{mid}` with `{"message":...}`
* `POST https://v2.groupme.com/messages/{cid}/{mid}` with `{"message":...}`
* `PATCH /v3/conversations/{cid}/messages/{mid}`
* `POST /v3/groups/{gid}/messages/{mid}/edit`

### Direct messages

| | |
|---|---|
| `GET /chats?page=N&per_page=20` | List DM conversations. |
| `GET /direct_messages?other_user_id={uid}&limit=N&before_id=&since_id=` | Fetch DM history. |
| `POST /direct_messages` | Send DM. Body wrapped like group send: `{"direct_message": {source_guid, recipient_id, text, attachments?}}`. |

DM **conversation_id** format: `"<lo>+<hi>"` where `lo` and `hi` are
the two participant user_ids sorted as integers (smaller first). Used
in `delete_message`, `edit_message`, `pin_message`, etc. Banter
constructs it via `MessageBubble._conversation_id()`.

### Reactions (likes)

The reactions endpoints overload the legacy "like" path. The
`conversation_id` segment is `gid` for groups, `<lo>+<hi>` for DMs.

| | |
|---|---|
| `POST /messages/{cid}/{mid}/like` | Plain heart (legacy). |
| `POST /messages/{cid}/{mid}/like` with body | Add a reaction (unicode or pack — see below). |
| `POST /messages/{cid}/{mid}/unlike` | Remove your reaction. Server figures out which from your user id. |

#### Reaction body shapes

**Unicode emoji:**
```json
{"like_icon": {"type": "unicode", "code": "👍"}}
```

**Pack (powerup) emoji:**
```json
{"like_icon": {"type": "emoji",
               "pack_id": "1",
               "pack_index": "42"}}
```

`pack_id` and `pack_index` **must be strings**. The server emits them
as strings on incoming reactions and rejects integer form with HTTP
400. JSON-encode with `ensure_ascii=False` so emoji bytes go on the
wire as raw UTF-8 (see "JSON body encoding" above).

**Known limitation:** historically the server has 500'd on many
non-heart unicode reactions from third-party clients. Setting the
`X-Requested-With: GroupMeWeb/1.2.3` header solved most of these. See
`project_reactions_limitation.md` if regressions return.

### Pinned messages (undocumented, v3)

Recovered 2026-04-30. Pin/unpin go through `/conversations/`, list
endpoints don't.

| | |
|---|---|
| `POST /v3/conversations/{cid}/messages/{mid}/pin` | Empty body. `response: null`. |
| `POST /v3/conversations/{cid}/messages/{mid}/unpin` | Empty body. `response: null`. |
| `GET /v3/pinned/groups/{gid}/messages` | `→ {count, messages: [...]}`. |
| `GET /v3/pinned/direct_messages?other_user_id={uid}` | `→ {count, direct_messages: [...]}`. |

* **Pinned-list responses carry `pinned_at` (unix s) and `pinned_by`
  (uid).** The regular `/groups/{gid}/messages` and `/direct_messages`
  endpoints do **not** inline these fields, so any client-side pin
  state must come from the dedicated list endpoints. Banter fetches the
  list once per conversation open into `ChatView._pinned_ids`.
* **Push stream does not carry pin events** as of 2026-04-30. Refetch
  the list after a pin/unpin action.
* **Permissions:** group owners can set a "Who can edit?" preference to
  *admins-only* or *everyone*. Non-permitted users get a server error
  on pin/unpin. Banter shows the action unconditionally and surfaces a
  toast on failure.

### Mentions

GroupMe encodes `@`-mentions as a single attachment on the message:

```json
{"type": "mentions",
 "user_ids": ["123", "456"],
 "loci":     [[10, 5], [20, 7]]}
```

Each `loci` entry is `[start_offset, length]`; `user_ids[i]` matches
`loci[i]`. Offsets are **code points**, not bytes (Python slice-
compatible for ASCII; the web client appears to count code points for
non-ASCII).

**`@everyone` is server-side.** Banter's compose code only inserts the
literal string `@everyone`. The GroupMe server scans the message text
on POST and, if it finds the literal string, automatically appends its
own attachment with `user_id: -1` and a locus over that range. Sending
a client-side broadcast attachment results in a duplicate entry in the
response (the server keeps both side-by-side). Don't expand
`@everyone` client-side.

### Contacts and blocks

| | |
|---|---|
| `GET /contacts` | Sync contact list. |
| `POST /blocks?user=<me>&otherUser=<them>` | Block. Note: query params, not body. |
| `DELETE /blocks?user=<me>&otherUser=<them>` | Unblock. |
| `GET /users/search?query=...` | Lookup users by phone/email — used by Add Member. |

### Gallery (group images)

```
GET /v3/conversations/{gid}/gallery?limit=N&acceptFiles=1&before=<gallery_ts>&after=<gallery_ts>
```

Returns messages that contain images and videos, newest-first,
paginated by `gallery_ts` (an ISO-8601 timestamp string). Response
shape varies — sometimes `{messages: [...]}`, sometimes a flat list.
Banter handles both in `get_gallery`.

### Albums (undocumented, v3)

Recovered 2026-05-03. Albums live under `/v3/conversations/{cid}/albums/`
and accept group ids for cid. Albums hold both images and videos;
older stub methods that assumed image-only have been replaced.

| | |
|---|---|
| `POST /v3/conversations/{cid}/albums/create` | Body: `{title, cover_attachment_id}`. `cover_attachment_id` is a media URL despite its name; empty string means no cover and the server auto-fills it from the first added media. Response carries `album_id`, `share_url`, `total_images`, `total_videos`. |
| `GET /v3/conversations/{cid}/albums?per_page=N` | List the conversation's albums. Response: `{albums: [...], next_cursor}`. |
| `GET /v3/conversations/{cid}/albums/{aid}/media?per_page=N` | Fetch one album. Response: `{album: {..., attachments: [...]}, next_cursor}`. Note the media list is nested inside `album.attachments`, not at top level. |
| `POST /v3/conversations/{cid}/albums/media?album_id={aid}` | Add media. Body is an **array** of `{media_url, media_type, media_source}`. `media_type` is `"image"` or `"video"`; `media_source` is always `"album"`. |
| `PUT /v3/conversations/{cid}/albums/update` | Update an album's title / cover. Captured 2026-05-03 but Banter doesn't wire it yet (no edit-album UI). |

### Calendar events

| | |
|---|---|
| `GET /v3/conversations/{gid}/events/list` | List events. |
| `POST /v3/conversations/{gid}/events/create` | Body: `{name, start_at, end_at, timezone, is_all_day, location?: {name}}`. `start_at`/`end_at` are ISO-8601 strings; both must be present together. |
| `POST /v3/conversations/{gid}/events/{event_id}/rsvps` | RSVP. Body: `{"rsvp": {"status": "going" \| "not_going"}}`. |
| `GET /v3/conversations/{gid}/events/{event_id}/rsvps` | RSVP list. |

### Polls

| | |
|---|---|
| `GET /v3/poll/{gid}` | List polls in a group. |
| `POST /v3/poll/{gid}` | Create. Body: `{subject, options: [{title}], expiration: <secs>, type: "single_choice"\|"multi_choice", visibility: "public"}`. |
| `POST /v3/poll/{gid}/{poll_id}/votes` | Cast vote(s). Body: `{options: [option_id, ...]}`. |

### Powerups (emoji packs)

```
GET https://powerup.groupme.com/powerups
```

Different host. Returns the **full historical catalog** (20+ packs
including some retired ones). Response shape varies — flat list, or
`{powerups: [...]}`, or wrapped in the standard `{meta, response}`
envelope with the list either at `response` or `response.powerups`.
`get_powerups` handles all four shapes.

Pack metadata gotchas:

* Top-level `id` is a **string** like `"emoji-groupme"` — ignore.
* `meta.pack_id` is the **integer** id used in reactions (1, 2, 3 …).
* `meta.transliterations` is a list of per-emoji names; emoji count is
  `len(meta.transliterations)`. There is no `pack_size` field.
* `meta.inline` is a **list of per-DPI variants** (not a dict). Each
  has `image_url`, `x`, `y`, `density`. Banter prefers `density=320`
  (xhdpi, 40×40 cells).
* `meta.icon` is the same shape, for the pack's own icon.
* Spritesheet layout is auto-detected from `sheet_width // cell_width`.
* No 80-cell cap — packs go up to ~101 emoji ("Back to School").

**Picker mismatch:** the official client shows a smaller, user-scoped
list. Likely `GET /users/me/powerups` or a filtered variant. Cross-
rendering between clients works because the server accepts any valid
`pack_id`.

### Image upload

```
POST https://image.groupme.com/pictures?token=<token>
Content-Type: image/jpeg | image/png | image/gif | image/webp
<raw bytes>
```

Different host, no JSON wrapper, raw image body. Response:

```json
{"payload": {"url": "https://i.groupme.com/<wxh>.<ext>.<hash>"}}
```

Use the returned URL as an `attachments: [{"type":"image", "url": ...}]`
entry on a subsequent send/edit.

### File upload (non-image attachments, undocumented)

Recovered 2026-04-30. Lives at `file.groupme.com`. Three-step flow.
The `{cid}` segment is the same conversation_id used by edit / delete /
pin: `group_id` for groups, `<lo>+<hi>` (sorted participant ids) for
DMs.

**1. Upload the bytes:**
```
POST https://file.groupme.com/v1/{cid}/files?name=<urlencoded filename>
Content-Type: <file mime>   (application/octet-stream is fine)
X-Access-Token: <token>
X-Requested-With: GroupMeWeb/1.2.3

<raw bytes>
```
Returns 201 + JSON containing `file_id` (and/or `status_url`). The
`file_id` doubles as the upload job id.

**2. Poll completion:**
```
GET https://file.groupme.com/v1/{cid}/uploadStatus?job=<file_id>&cnt=<N>
```
`cnt` is a 0-indexed counter the web client increments per poll.
Returns `{"status": "completed", "file_id": "..."}` when ready.

**3. Send the message** via the standard endpoint:

* Groups: `POST /v3/groups/{gid}/messages` with body wrapped in
  `{"message": {...}}`.
* DMs: `POST /v3/direct_messages` with body wrapped in
  `{"direct_message": {...}}`.

In both cases the attachment is the same:
```json
{"type": "file", "file_id": "<uuid>"}
```

**Resolve metadata for received files:**
```
POST https://file.groupme.com/v1/{cid}/fileData
Content-Type: application/json

{"file_ids": ["<uuid>", ...]}
```
Returns:
```json
[{"file_id": "<uuid>", "meta": 200,
  "file_data": {"file_name": "...", "file_size": <bytes>, "mime_type": "..."}}]
```

The message attachment itself only carries `file_id` — `file_name`,
`file_size`, and `mime_type` must be fetched separately. Banter calls
this lazily from `FileAttachment._fetch_metadata`.

**Download URL** (verified 2026-04-30):
```
GET https://file.groupme.com/v1/{cid}/files/{file_id}?access_token=<token>&_dl=<unix_ms>
```

* The query parameter is **`access_token`**, not `token` like other
  endpoints. Using `token=` returns 401.
* `_dl=<unix_ms>` is a cache buster the web client adds; harmless to
  include and helps bypass intermediary caches.
* Response carries `Content-Disposition: attachment; filename*=UTF-8''<encoded>`
  with the original filename, so the server-suggested filename will
  match the upload.

### Calls (Microsoft Teams, undocumented)

Recovered 2026-05-03. **GroupMe calls aren't WebRTC.** The server
returns an Azure Communication Services token plus a
`teams.live.com/meet/...` URL — the official client embeds Teams'
call composite to actually join. From a Linux app without that SDK
the right move is to launch the meeting URL in the user's browser,
which has working camera/mic prompts and the full Teams UI.

| | |
|---|---|
| `GET /v3/conversations/{cid}/call` | Get-or-create the call session. Returns `{token, expires_on, meeting_type: "tfl", meeting_id: "https://teams.live.com/meet/..."}`. The same endpoint lazily creates a session if none exists, so a single GET both starts a call and joins one already in progress. |
| `PUT /v1/conversations/{cid}/call/heartbeat` | Keepalive sent by the official client during an active call. Banter doesn't send heartbeats — the call lives in the user's browser, not in the app. Note the **v1** path. |
| `POST /v1/conversations/{cid}/call/disconnect` | Leave-call signal. Same: only relevant if Banter were the call host, which it isn't. |

Banter's `_on_call_clicked` calls `GET /call` and hands `meeting_id`
to `Gio.AppInfo.launch_default_for_uri`. No in-app participants UI,
no media plane, no signaling beyond fetching the meeting URL.

---

## Push (Faye/Bayeux WebSocket)

`push.groupme.com/faye` speaks the [Bayeux 1.0 protocol][bayeux] over
a WebSocket. No external library — `push.py` is hand-rolled WS framing
+ Faye protocol JSON, all stdlib.

[bayeux]: https://docs.cometd.org/current8/reference/#_bayeux

### Connection lifecycle

```
TCP+TLS → ws://push.groupme.com:443/faye (HTTP Upgrade)
   → Faye /meta/handshake             ← server returns clientId
   → Faye /meta/subscribe /user/{uid} ← own-user channel
   → Faye /meta/subscribe /group/{gid} … (one per joined group)
   → Faye /meta/connect (connectionType=websocket)
       ← server holds the connection ~10–30 s, pushes events,
          then closes the WebSocket cleanly
   → reopen WebSocket and repeat from /meta/handshake
```

The server closing the WebSocket is **expected and normal** —
GroupMe's Faye is "long-poll over WebSocket". Reconnect immediately,
no backoff. Only TCP-level / handshake errors trigger backoff.

### Subscribed channels

* `/user/{my_user_id}` — own-user feed: `line.create`, edits, deletes,
  reactions, system events.
* `/group/{group_id}` — one per joined group. Carries:
  * `typing` events (only on the group channel — not on `/user/`)
  * Possibly duplicate `line.create` events (de-duped client-side via
    `_bubble_map`).
* `/direct_message/<lo>_<hi>` — one per DM the user opens during the
  session. Carries `typing` events for that DM. Note the **underscore**
  separator — HTTP `conversation_id` uses `+`, but Faye disallows that
  in channel names. Subscribed lazily on DM open and never unsubscribed.

`push.py` forwards events from `/user/`, `/group/`, and
`/direct_message/` channels. ChatView's `_on_push_event` dispatches
by `data.type`.

### Inbound event shapes

**`line.create`** — new message. Wrapped:
```json
{"type": "line.create",
 "subject": { ...message dict (id, text, group_id, user_id, attachments, ...) }}
```

**`line.update` / `message.update` / `line.edit`** — edit. Same wrapped
shape; the new message body is at `subject.line` or directly at
`subject`.

**`line.destroy` / `line.delete`** — delete. ChatView removes the
matching bubble from the UI.

**`like.create` / `like.delete` / `favorite.create` / `favorite.destroy`
/ `reaction.create` / `reaction.destroy`** — all the variants the
server emits. Body looks like:
```json
{"type": "like.create",
 "subject": {"line": {<message>},
             "reactions": [{type, code|pack_id+offset, user_ids}, ...],
             "user_reaction": {...}}}
```

**`typing`** — flat (no `subject` envelope), seen on both
`/group/{gid}` and `/direct_message/<lo>_<hi>`:
```json
{"type": "typing",
 "user_id": "<sender>",
 "started": <ms_unix_timestamp>}
```

There is no "stopped" event. Convention: sender re-pulses every ~3 s
while typing; receivers auto-clear after ~5 s without a follow-up
pulse, or on a new message from the same sender.

### Outbound publish (typing)

```json
[{"channel":"/group/{gid}"  OR  "/direct_message/<lo>_<hi>",
  "data":{"type":"typing","user_id":"<me>","started":<ms_unix>},
  "clientId":"<faye_client_id>",
  "id":"<seq>",
  "ext":{"access_token":"<token>"}}]
```

Outer is an array (Bayeux requirement). `clientId` is the Faye id from
handshake, **not** your user_id. `id` is a per-client sequence number.
`ext.access_token` carries the GroupMe OAuth token.

### Threading model

`push.py` runs the WebSocket recv loop on a single daemon thread. Main-
thread publishes (typing pulses, live `subscribe_group`) call into
`_ws_send` which is wrapped in `self._send_lock`; otherwise concurrent
`sendall` calls would interleave bytes and corrupt the WebSocket
framing.

Live `subscribe_group` (called when a new group is loaded after the
worker thread has already passed its initial subscribe loop) sends the
`/meta/subscribe` frame **fire-and-forget** — it doesn't `recv()` the
ack, because racing the worker's recv would cause torn frames. The ack
is consumed by the worker's recv loop and harmlessly dropped (it
doesn't match the `/user/` or `/group/` event-forwarding filter).

### What does NOT come through push (as of 2026-05-01)

* `pin` / `unpin` events

---

## DM/group conversation_id reference

Quick reference for which id format goes where:

| Endpoint kind | Group id | DM id |
|---|---|---|
| `/groups/{gid}/...`   | `group_id` | n/a |
| `/direct_messages?other_user_id=X` | n/a | other user's id |
| `/conversations/{cid}/...` (delete, edit-DM, pin, unpin) | `group_id` | `"<lo>+<hi>"` (sorted user ids) |
| `/messages/{cid}/{mid}/like` | `group_id` | `"<lo>+<hi>"` |
| `/v3/pinned/groups/{gid}/messages` | `group_id` | n/a |
| `/v3/pinned/direct_messages?other_user_id=X` | n/a | other user's id |

Faye channels:

| | |
|---|---|
| `/user/{my_user_id}` | own feed, all groups + DMs |
| `/group/{gid}` | per-group feed (typing, etc.) |
| `/direct_message/<lo>_<hi>` | per-DM feed (typing) — note `_` separator, NOT `+` |

---

## What's deliberately not implemented

* **In-app voice / video calls.** Calls are reachable via the
  call-start button in chat headers, but the actual media goes
  through the user's browser via the Teams meeting URL — Banter
  doesn't embed a call composite. A real in-app implementation
  would mean Azure Communication Services Web SDK (closed-source)
  or building a WebRTC pipeline against the same SFU the official
  client uses. Worth adding a passive "Call in progress" banner
  driven by the `meeting_id` returned by `GET /call`, but that
  hasn't shipped yet.
* **Voice-message attachment shape.** Banter sends recorded audio
  through `upload_file`, which makes recipients see a generic file
  download rather than an inline voice clip. The dedicated
  voice-message upload endpoint isn't captured. Capture a HAR while
  sending a voice clip from the web client to learn the right
  endpoint.
* **Album edit / delete UI.** `PUT /v3/conversations/{cid}/albums/update`
  is captured (see Albums above) but no Banter UI exposes it. No
  delete endpoint has been captured.
* **GIF picker** — would use Tenor/Giphy directly, not GroupMe.
* **Read receipts.** `POST /v3/conversations/{cid}/{mid}/read_receipt`
  and `POST /v3/conversations/{cid}/read_receipt` are visible in
  HARs but Banter doesn't send or display them yet.
* **Pin events on push** — server doesn't seem to emit them; pin state
  refetched on demand.
* **`/users/me/powerups` (or whatever the user-scoped catalog is)** —
  exact endpoint not yet captured.
* **UnifiedPush relay** — would need an external relay service holding
  the user's Faye token; intentionally deferred.

---

*Last verified against `web.groupme.com` traces on 2026-05-03. If
something here stops working, capture a HAR from the official web
client first and compare.*
