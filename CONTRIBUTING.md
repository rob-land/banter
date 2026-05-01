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
sudo dnf install python3 python3-gobject gtk4 libadwaita meson ninja-build \
                 desktop-file-utils appstream
```

On Ubuntu/Debian:
```bash
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
                 gir1.2-adw-1 meson ninja-build desktop-file-utils appstream
```

### Build and run (development)

```bash
# Configure build directory
meson setup _build --prefix=/usr

# Compile and install to a staging root
meson install -C _build --destdir /tmp/banter-root

# Run from the staging root
PYTHONPATH=$(echo /tmp/banter-root/usr/lib/python*/site-packages) \
  /tmp/banter-root/usr/bin/banter
```

### Building the Flatpak

```bash
# Install flatpak-builder
sudo dnf install flatpak-builder   # Fedora
sudo apt install flatpak-builder   # Debian/Ubuntu

# Add GNOME runtime
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install flathub org.gnome.Platform//49 org.gnome.Sdk//49

# Build and install locally
flatpak-builder --install --user --force-clean _flatpak land.rob.Banter.json

# Run
flatpak run land.rob.Banter
```

## Project structure

See the "Project structure" section of [README.md](README.md) for the
canonical file layout. New `.py` files in `src/` must be listed in
`src/meson.build` (`banter_sources` / `widget_sources` / `dialog_sources`)
or they won't ship with the install.
