# Banter — Quick start

A native GTK4 / libadwaita client for GroupMe. Sign in once, then
chat from your desktop or Phosh phone like any other GNOME app.

## Install

From the rob-land Flatpak repo:

```bash
flatpak remote-add --user --if-not-exists rob-land \
    https://flatpak.rob.land/rob-land.flatpakrepo
flatpak install --user rob-land land.rob.banter
```

Or build from source — see the project README.

## First-time setup

Launch Banter. You'll see a one-button **Sign in with browser**
screen. Click it and Banter will:

1. Open `web.groupme.com` in your default browser.
2. Wait for GroupMe to redirect to a local callback at
   `http://localhost:7654` after you sign in.
3. Capture the token, close the browser tab, and bring Banter to
   the foreground.

That's the entire onboarding. There's no manual token-paste flow.

## Daily use

The main window is a two-pane shell:

- **Sidebar** — Chats (groups + DMs) and Contacts, switchable via
  the top tabs / bottom switcher on phone.
- **Content** — message history for the selected conversation,
  with a compose bar at the bottom.

A few things worth knowing about:

- **Search** in the sidebar header filters groups and contacts by
  name as you type.
- **Mute** a conversation via the bell icon in its header. The
  menu has three presets (1 hour, 8 hours, "until I turn it back
  on"); muted conversations get a slashed-bell glyph in the
  sidebar.
- **Reactions and replies** — long-press / right-click a message
  bubble for the action sheet (emoji picker, reply, copy).
- **Voice messages** — hold the mic button in the compose bar to
  record; release to send.
- **Calls** — group calls open in your default browser (GroupMe
  uses Microsoft Teams meetings under the hood).

## Multi-account

Banter supports more than one signed-in GroupMe account at a time.

- Switch via the **avatar menu** in the sidebar header.
- **Manage Accounts** opens a dialog where you can add another
  account (same browser-OAuth flow), switch the active one, or
  sign out individually.

Each account has its own watch state, mute settings, and drafts.

## Running in the background (notifications without the window)

See [`background.md`](background.md) for the full guide. Short
version: enable **Run in background** in Preferences (⋮ menu →
Preferences) so closing the window keeps Banter loaded and
delivering message notifications. Add **Run at startup** if you
want it to launch with your session.

## Where things are kept

| What | Path |
| --- | --- |
| Account metadata (names, mute lists) | `~/.var/app/land.rob.banter/config/banter/config.json` (Flatpak) — 0600 |
| OAuth tokens | libsecret (`gnome-keyring`/`kwallet`/etc.) |
| Image / video cache | `~/.var/app/land.rob.banter/cache/banter/` |
| Logs | `~/.var/app/land.rob.banter/data/banter/banter.log` (rotating) |
| Autostart entry (if enabled) | `~/.config/autostart/land.rob.banter.desktop` |

Run with `--debug` to bump log verbosity to DEBUG. The log file is
always written regardless of `--debug`; the flag just changes the
level.

## Notable limits

- **No translations yet** — i18n infrastructure exists but no
  message catalogs ship today.
- **QR sharing** in the Share Group dialog needs the optional
  `qrcode` pip package. Without it the dialog still works, just
  with the URL only.
- **GroupMe API is reverse-engineered** from the web client.
  Endpoints can break without notice; if a feature suddenly stops
  working, the API probably changed. File an issue and we'll
  re-capture the call.
