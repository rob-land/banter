# Banter — CLAUDE.md

## What this project is

A native GNOME client for GroupMe, written in Python with GTK4 and libadwaita. App ID: `land.rob.banter`.

## Code quality

A core goal is well-structured, readable code that follows idiomatic Python (PEP 8) and GNOME / libadwaita conventions; the cohort-shared [`STYLE_GUIDE.md`](STYLE_GUIDE.md) layers on top. When existing code doesn't meet that bar, refactor rather than perpetuate the pattern.

## Before making changes

Read [`STYLE_GUIDE.md`](STYLE_GUIDE.md) first when touching any of:

- Meson build files, the Flatpak manifest, or `requirements.txt`
- Anything under `data/ui/` or `data/icons/`
- New top-level Python files, or new modules under `src/<pkg>/`
- Imports — especially `import gi` / `gi.require_version`
- New launcher / `.in` substitution targets

The five-project unification (banter, clicker, finlit, jamjar, tonic)
established conventions that drift easily from intuition. The recurring
slip is reintroducing per-file `gi.require_version` blocks in new
modules; the launcher (`<project>.in`) is the single declaration site.

## Tech stack

- **Language**: Python 3.11+
- **UI toolkit**: GTK4 + libadwaita (via PyGObject / GObject Introspection). Blueprint (`.blp`) under `data/ui/` for the structures that have been migrated (the main window shell, the help-overlay, the jump-to-date dialog, plus `style.css`); the rest is still constructed programmatically (every `widgets/*.py` and most `dialogs/*.py`). Convert to `.blp` opportunistically — don't block on a flag-day rewrite.
- **Build system**: Meson + Ninja (Blueprint compile + GResource bundle live in `data/ui/meson.build`)
- **Packaging**: Flatpak (manifest: `build-aux/flatpak/land.rob.banter.json`)
- **Target platforms**: x86_64 desktop, aarch64 (Furi FLX1s, Raspberry Pi 5)
- **GNOME Platform**: 50

## Source layout

```
src/
├── application.py      BanterApplication (Adw.Application), startup/activate, About + Shortcuts dialogs
├── api.py              GroupMe REST API client (urllib, no third-party HTTP lib)
├── async_utils.py      run_in_background helper for worker threads
├── background_portal.py  org.freedesktop.portal.Background helper for autostart toggle
├── config.py           Accounts and preferences (multi-account support)
├── constants.py        APP_ID, APP_VERSION, DEBUG/DEMO/BACKGROUND flags, dbg()/log() helpers, OAUTH_PORT
├── helpers.py          Image loading and caching
├── mock_api.py         Demo-mode fixture used by --demo (no network)
├── notifications.py    NotificationDispatcher — Gio.Notification I/O + mute/call-event routing
├── notifier.py         BanterNotifier — headless --background daemon (push + REST catch-up + notify)
├── oauth.py            OAuth sign-in dialog + local callback server; BANTER_CLIENT_ID lives here
├── push.py             WebSocket push client (Bayeux/Faye protocol)
├── secrets.py          libsecret token storage
├── window.py           BanterWindow — sidebar + content split, primary menu, call launcher
├── widgets/
│   ├── base.py             StandardDialog base for Adw.Dialogs
│   ├── chat_view.py        Message list, compose bar, voice-message recording
│   ├── conversation_row.py Sidebar rows (groups + DMs)
│   ├── event_card.py       Inline event card in messages
│   ├── mention_popover.py  @-mention autocomplete
│   ├── message_bubble.py   Bubbles, emoji reactions, reply quotes
│   ├── misc.py             ImageAttachment, VideoAttachment, FileAttachment, DateSeparator, LoadingRow
│   ├── poll_card.py        Inline poll card with vote/results
│   └── reactions_sheet.py  Unified emoji picker + reactors view
└── dialogs/
    ├── accounts.py     Multi-account switcher (AdwPreferencesDialog)
    ├── events.py       Create / list / show event, create poll
    ├── gallery.py      Group gallery + album creator / viewer / picker
    ├── group.py        New group, contact detail, add-to-group
    ├── jump_to_date.py Calendar picker for chat history navigation
    ├── members.py      Member list (AdwPreferencesDialog)
    ├── pinned.py       Pinned messages list
    └── settings.py     Group settings (AdwPreferencesDialog)
```

## Key conventions

- No third-party HTTP libraries — `api.py` uses `urllib` directly.
- Debug logging is gated behind the `DEBUG` constant; use `dbg()` for verbose output, `log()` for normal output (both defined in `constants.py`).
- GObject signals and async UI updates follow standard GTK4/libadwaita patterns.
- OAuth callback uses `http://localhost:7654`; client ID is `BANTER_CLIENT_ID` in `oauth.py`.
- New GroupMe features are HAR-driven: capture from `web.groupme.com`, document the endpoint in `GROUPME_API.md`, wire it in `api.py`, add UI. Don't guess endpoints. See the file for the patterns Banter has captured (pin/unpin, edit, albums, calls, file upload).
- GStreamer is used for two things: voice recording (`autoaudiosrc → opusenc → oggmux → filesink` in `chat_view.py`) and inline video playback (`Gtk.Video` in `widgets/misc.py`). The Flatpak manifest grants `--socket=pulseaudio` for the recording path.
- GroupMe calls are Microsoft Teams meetings — `_on_call_clicked` opens the meeting URL via `Gio.AppInfo.launch_default_for_uri` rather than embedding a call composite. **Don't** try to add WebRTC; the official client uses the closed-source ACS Web SDK.
- `banter --background` runs as a headless notification daemon (`BanterNotifier`): no window, holds the GApplication via `app.hold()`, owns the Faye WebSocket + a REST catch-up that compares against a per-account watermark persisted in `Config.{get,set}_last_seen_map`. First-ever run with an empty watermark seeds quietly to avoid flooding on initial autostart. Notification clicks re-enter `_on_activate`, which builds the window and retires the notifier. Autostart is opt-in via the **Notifications** group in the Accounts preferences dialog → `org.freedesktop.portal.Background` `RequestBackground`.

## Build workflow

```bash
# Native (development)
meson setup _build --prefix=/usr
meson install -C _build --destdir /tmp/banter-root
PYTHONPATH=/tmp/banter-root/usr/share/banter /tmp/banter-root/usr/bin/banter

# Flatpak (x86_64)
flatpak-builder --install --user --force-clean _flatpak build-aux/flatpak/land.rob.banter.json
flatpak run land.rob.banter

# Flatpak (aarch64 cross-compile)
flatpak-builder --arch=aarch64 --install --user --force-clean _flatpak-aarch64 build-aux/flatpak/land.rob.banter.json
```

## Things to watch out for

- QR code support in the Share Group dialog requires the optional `qrcode` pip package (not bundled by default).
- Translations directory (`po/`) exists but is empty — i18n not yet implemented.
