# Contributing to Banter

## Building from source

### Prerequisites

- Python 3.11+
- GTK 4.12+
- libadwaita 1.4+
- PyGObject 3.46+
- Meson 0.62+
- Ninja

On Fedora:
```bash
sudo dnf install python3 python3-gobject gtk4 libadwaita webkit2gtk4.1 meson ninja-build
```

On Ubuntu/Debian:
```bash
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
  gir1.2-adw-1 gir1.2-webkit-6.0 python3-gi meson ninja-build
```

### Build and run (development)

```bash
# Configure build directory
meson setup _build --prefix=/usr

# Compile and install to a local prefix
meson install -C _build --destdir /tmp/banter-install

# Run directly from source (development mode)
PYTHONPATH=. python3 -m banter.application
```

### Building the Flatpak

```bash
# Install flatpak-builder
sudo dnf install flatpak-builder   # Fedora
sudo apt install flatpak-builder   # Debian/Ubuntu

# Add GNOME runtime
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install flathub org.gnome.Platform//46 org.gnome.Sdk//46

# Build and install locally
flatpak-builder --install --user --force-clean _flatpak land.rob.Banter.json

# Run
flatpak run land.rob.Banter
```

## Project structure

```
banter/
├── meson.build                  Root build file
├── land.rob.Banter.json         Flatpak manifest
├── data/
│   ├── meson.build
│   ├── land.rob.Banter.desktop.in
│   ├── land.rob.Banter.appdata.xml.in
│   ├── land.rob.Banter.gschema.xml
│   └── icons/hicolor/scalable/apps/land.rob.Banter.svg
├── po/                          Translations
│   ├── meson.build
│   └── LINGUAS
└── src/
    ├── meson.build
    ├── banter.in                Launcher script template
    ├── __init__.py
    ├── application.py           BanterApplication (Adw.Application)
    ├── api.py                   GroupMeAPI REST client
    ├── config.py                Persistent config + account storage
    ├── constants.py             App-wide constants, logging, APP_ID
    ├── css.py                   Application stylesheet (APP_CSS)
    ├── helpers.py               Image loading/caching utilities
    ├── main.py                  Entry point
    ├── oauth.py                 OAuth flow + local callback server
    ├── push.py                  GroupMePush WebSocket client
    ├── window.py                MainWindow
    ├── widgets/
    │   ├── chat_view.py         ChatView (message list + compose)
    │   ├── conversation_row.py  Sidebar rows (ConversationRow etc.)
    │   ├── message_bubble.py    MessageBubble + reactions
    │   └── misc.py              ImageAttachment, DateSeparator, LoadingRow
    └── dialogs/
        ├── accounts.py          AccountsDialog
        ├── events.py            CreateEventDialog, CreatePollDialog
        ├── gallery.py           GalleryDialog
        ├── group.py             GroupDetailDialog, NewGroupDialog
        ├── login.py             LoginDialog (OAuth)
        ├── members.py           MembersDialog
        └── settings.py         GroupSettingsDialog, PreferencesDialog
```
