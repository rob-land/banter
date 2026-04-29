# Banter

A native GNOME client for GroupMe, built with GTK4 and libadwaita.

> ⚠️ **Honest disclosure**: Every line of code in this project was written by
> [Claude.ai](https://claude.ai) (Anthropic's AI assistant). I apologise in
> advance for the AI slop. Pull requests that fix the inevitable weird
> decisions are very welcome.

---

## Features

- **Real-time messaging** via GroupMe's WebSocket push API — new messages
  appear instantly without polling
- **Groups and direct messages** unified in a single sidebar, sorted by
  most recent activity
- **Compose tools**: per-conversation drafts (preserved on chat switch),
  `@`-mention autocomplete with a server-detected `@everyone`, and
  quote-reply with an inline preview
- **Edit and delete your own messages** — long-press / right-click the
  bubble, with edit gated to GroupMe's server-side time window
- **In-conversation search** (Ctrl+F or magnifying-glass header button)
  with prev/next navigation and a live "X of Y" match counter
- **Per-conversation mute** with a header bell toggle and a sidebar
  indicator on muted rows
- **Emoji reactions** with hover tooltips showing who reacted, a detail
  dialog listing all reactions, and a picker for adding your own
- **Reply threads** — messages that reply to an earlier message show a
  quoted preview of the original
- **Image attachments** with lazy loading and a full gallery view per group
- **Clickable links** — URLs and email addresses in messages are rendered
  as tappable links
- **Contacts tab** populated automatically from group members, tap any
  contact to open a direct message
- **Group management** — member list, group settings, create events
  and polls
- **Share group** — copy invite link, system share sheet, or scan a QR code
- **OAuth sign-in** via the system browser — no password is ever stored by
  the app
- **Desktop notifications** with click-to-focus, suppressed for muted
  conversations
- **Adaptive layout** — works on both desktop and mobile (tested on FuriOS
  on the Furi FLX1s and Debian on the Raspberry Pi 5)
- **GNOME Platform 49** — follows libadwaita conventions, respects the
  system colour scheme (light/dark)

---

## Building from source

### Dependencies

| Package | Fedora | Debian/Ubuntu |
|---|---|---|
| Python 3.11+ | `python3` | `python3` |
| PyGObject | `python3-gobject` | `python3-gi python3-gi-cairo` |
| GTK 4 | `gtk4` | `gir1.2-gtk-4.0` |
| libadwaita | `libadwaita` | `gir1.2-adw-1` |
| Meson | `meson` | `meson` |
| Ninja | `ninja-build` | `ninja-build` |
| desktop-file-utils | `desktop-file-utils` | `desktop-file-utils` |
| appstream | `appstream` | `appstream` |

**Fedora:**
```bash
sudo dnf install python3 python3-gobject gtk4 libadwaita \
                 meson ninja-build desktop-file-utils appstream
```

**Debian / Ubuntu:**
```bash
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
                 gir1.2-adw-1 meson ninja-build desktop-file-utils appstream
```

### Build and install

```bash
# Clone the repository
git clone https://codeberg.org/robland/banter.git 
cd banter

# Configure
meson setup _build --prefix=/usr

# Build and install (to a staging root, not your live system)
meson install -C _build --destdir /tmp/banter-root

# Run directly from the staging root
PYTHONPATH=$(echo /tmp/banter-root/usr/lib/python*/site-packages) \
  /tmp/banter-root/usr/bin/banter
```

To install system-wide (not recommended for development):
```bash
sudo meson install -C _build
```

---

## Building the Flatpak

### Prerequisites

```bash
# Fedora
sudo dnf install flatpak flatpak-builder

# Debian / Ubuntu
sudo apt install flatpak flatpak-builder
```

Add Flathub and install the GNOME 49 runtime:
```bash
flatpak remote-add --if-not-exists flathub \
  https://dl.flathub.org/repo/flathub.flatpakrepo

flatpak install flathub org.gnome.Platform//49 org.gnome.Sdk//49
```

### Build and run (x86_64)

```bash
flatpak-builder --install --user --force-clean \
  _flatpak land.rob.Banter.json

flatpak run land.rob.Banter
```

---

## Building the Flatpak for aarch64

Cross-compiling to aarch64 (for devices like the Furi FLX1s or Raspberry
Pi) requires QEMU user-mode emulation and the aarch64 GNOME runtime.

### Install QEMU and the aarch64 runtime

**Fedora:**
```bash
sudo dnf install qemu-user-static
```

**Debian / Ubuntu:**
```bash
sudo apt install qemu-user-static binfmt-support
```

Install the aarch64 runtime (this downloads ~1 GB):
```bash
flatpak install flathub --arch=aarch64 \
  org.gnome.Platform//49 org.gnome.Sdk//49
```

### Build for aarch64

```bash
flatpak-builder --arch=aarch64 --install --user --force-clean \
  _flatpak-aarch64 land.rob.Banter.json
```

### Export a redistributable bundle

```bash
# Build into a local repo
flatpak-builder --arch=aarch64 --repo=repo --force-clean \
  _flatpak-aarch64 land.rob.Banter.json

# Export a single-file bundle you can sideload onto the device
flatpak build-bundle repo --arch=aarch64 \
  banter-aarch64.flatpak land.rob.Banter
```

Transfer `banter-aarch64.flatpak` to the device and install:
```bash
flatpak install --user banter-aarch64.flatpak
```

---

## GroupMe application registration

Banter uses the bundled OAuth client ID. If you fork the project and
register your own application at
[dev.groupme.com/applications](https://dev.groupme.com/applications),
set the **Callback URL** to:

```
http://localhost:7654
```

Then update `BANTER_CLIENT_ID` in `src/oauth.py`.

---

## Optional: QR code support

The "Share Group" dialog can display a scannable QR code if the `qrcode`
Python package is installed:

```bash
pip install qrcode        # outside Flatpak
```

Inside a Flatpak build, add it to the manifest as a pip module.

---

## Project structure

```
banter/
├── meson.build                  Root build file
├── land.rob.Banter.json         Flatpak manifest
├── data/
│   ├── land.rob.Banter.desktop.in
│   ├── land.rob.Banter.appdata.xml.in
│   ├── land.rob.Banter.gschema.xml
│   └── icons/
│       └── hicolor/
│           ├── scalable/apps/land.rob.Banter.svg
│           └── symbolic/apps/land.rob.Banter-symbolic.svg
├── po/                          Translations (none yet)
└── src/
    ├── main.py                  Entry point invoked by the launcher script
    ├── application.py           BanterApplication (Adw.Application)
    ├── api.py                   GroupMe REST API client
    ├── async_utils.py           run_in_background worker-thread helper
    ├── config.py                Accounts and preferences
    ├── constants.py             App-wide constants and logging
    ├── css.py                   Application stylesheet
    ├── helpers.py               Image loading and caching, pack catalog
    ├── oauth.py                 OAuth sign-in dialog
    ├── push.py                  WebSocket push client (Bayeux/Faye)
    ├── window.py                Main window
    ├── widgets/
    │   ├── base.py              StandardDialog base class
    │   ├── chat_view.py         Message list and compose bar
    │   ├── conversation_row.py  Sidebar conversation rows
    │   ├── event_card.py        Inline event/poll cards in bubbles
    │   ├── mention_popover.py   @-mention autocomplete in compose bar
    │   ├── message_bubble.py    Bubbles, reactions, edit/delete/reply menu
    │   ├── misc.py              ImageAttachment, DateSeparator, LoadingRow
    │   └── reactions_sheet.py   Reaction picker (emoji + pack tabs)
    └── dialogs/
        ├── accounts.py          Multi-account switcher
        ├── events.py            Create event / create poll
        ├── gallery.py           Image gallery
        ├── group.py             Group detail and contact detail
        ├── login.py             Initial sign-in dialog
        ├── members.py           Member list
        └── settings.py          Group settings and preferences
```

---

## Contributing

Bug reports and patches are welcome. Since the codebase is AI-generated
there are almost certainly structural oddities, redundant code paths, and
questionable decisions throughout — please don't be shy about pointing
them out or rewriting things properly.

## License

GNU General Public License v3.0 — see [COPYING](COPYING).
