# Project Style Guide

Conventions Claude follows for native GNOME / Phosh apps in this
collection (banter, clicker, finlit, jamjar, tonic). Drop this file
into a new project alongside `CLAUDE.md` so Claude follows the same
patterns from day one.

## Identity

- **App-id namespace**: `land.rob.<Project>` (e.g. `land.rob.Clicker`).
  Use the same casing in every metadata file. Do not use reverse-DNS
  forms like `io.github.<user>.<App>`.
- **Project name** in lowercase: `clicker`, `finlit`, etc. — used as
  the Python package name, the launcher script name, and the systemd /
  Flatpak `command:` value.
- **License**: GPL-3.0-or-later, filename `COPYING` (not `LICENSE`).
- **Class prefix**: `<Project>Window`, `<Project>Application`,
  `<Project>SomePage`. Avoid bare `MainWindow`. Set
  `__gtype_name__ = "<Project><ClassName>"` on every `Gtk.Template`d
  class and on widgets exposed to GResource lookups.
- **GResource prefix**: `/land/rob/<Project>/...`.
- **GSettings schema id**: `land.rob.<Project>` (file:
  `data/land.rob.<Project>.gschema.xml`).

## Source layout

```
<project>/
├── meson.build                     # root build (defines APP_ID etc.)
├── meson_options.txt               # only if real options exist
├── COPYING                         # GPL-3.0-or-later
├── README.md                       # public-facing
├── CLAUDE.md                       # committed; no identifying info
├── requirements.txt                # Python runtime deps (one per line)
├── build-all.sh                    # multi-arch flatpak driver
├── fix-flatpak-deps.py             # tarball -> wheel patcher
├── build-aux/
│   └── flatpak/
│       ├── land.rob.<Project>.json # Flatpak manifest (JSON, not YAML)
│       └── python3-deps.json       # generated, gitignored
├── data/
│   ├── meson.build
│   ├── land.rob.<Project>.desktop.in
│   ├── land.rob.<Project>.metainfo.xml.in
│   ├── land.rob.<Project>.gschema.xml
│   ├── icons/hicolor/{scalable,symbolic}/apps/...svg
│   └── ui/
│       ├── meson.build             # blueprint-compiler + gresource
│       ├── land.rob.<Project>.gresource.xml
│       └── *.blp                   # one per template
├── po/
│   ├── LINGUAS
│   ├── POTFILES.in
│   └── meson.build
└── src/
    ├── meson.build
    ├── <project>.in                # processed to bin script by Meson
    ├── const.py.in                 # processed to const.py with paths
    └── <package>/
        ├── __init__.py
        ├── main.py                 # entry point
        ├── application.py          # Adw.Application subclass
        ├── window.py               # main window
        └── <subpackage>/...
```

The Python package lives under `src/<package>/`. Subpackages group
features (e.g. `devices/`, `discovery/`, `pages/`, `widgets/`,
`dialogs/`). Tests live in `tests/` if present; never in `src/`.

## Build system

- **Meson + Ninja**. Canonical root `meson.build`:
  ```meson
  project('<project>',
    version: '<x.y.z>',
    meson_version: '>= 1.0.0',
    license: 'GPL-3.0-or-later',
    default_options: ['warning_level=2'],
  )

  i18n   = import('i18n')
  gnome  = import('gnome')
  python = import('python').find_installation('python3')

  application_id = 'land.rob.<Project>'
  prefix     = get_option('prefix')
  bindir     = prefix / get_option('bindir')
  datadir    = prefix / get_option('datadir')
  localedir  = prefix / get_option('localedir')
  pkgdatadir = datadir / meson.project_name()
  moduledir  = python.get_install_dir() / meson.project_name()

  # Bare-string substitutions for XML/desktop/launcher .in files.
  conf = configuration_data()
  conf.set('PYTHON',     python.full_path())
  conf.set('APP_ID',     application_id)
  conf.set('VERSION',    meson.project_version())
  conf.set('PKGDATADIR', pkgdatadir)
  conf.set('LOCALEDIR',  localedir)

  # Quoted-string substitutions for Python .in files (paths/IDs need
  # to land as Python string literals).
  py_conf = configuration_data()
  py_conf.set('PYTHON',          python.full_path())
  py_conf.set_quoted('APP_ID',     application_id)
  py_conf.set_quoted('VERSION',    meson.project_version())
  py_conf.set_quoted('PKGDATADIR', pkgdatadir)
  py_conf.set_quoted('LOCALEDIR',  localedir)

  subdir('data')
  subdir('src/<package>')
  subdir('po')

  gnome.post_install(
    glib_compile_schemas:    true,
    gtk_update_icon_cache:   true,
    update_desktop_database: true,
  )
  ```
- The variable name is `application_id` (full word), not `app_id`.
- The conf keys are uppercase (`APP_ID`, `VERSION`, `PKGDATADIR`,
  `LOCALEDIR`, `PYTHON`).
- **Install Python sources** with
  `python.install_sources(sources, subdir: '<package>')`. Avoid
  `install_data(install_dir: moduledir)`.
- **Launcher script**: `src/<package>/<project>.in` configured by
  Meson into `bin/<project>`, sets `PYTHONPATH`/`GSETTINGS_SCHEMA_DIR`
  and execs `python3 -m <package>`.
- **Constants module**: `src/<package>/const.py.in` configured to
  `const.py` with the substituted paths. Do not hard-code
  `/usr/share/...` in Python.

## UI: Blueprint

- UI is defined in `data/ui/*.blp` (Blueprint), compiled to `.ui` at
  build time, bundled via GResource. Logic stays in Python.
- `data/ui/meson.build` does both the blueprint compile and the
  gresource bundle:
  ```
  blueprint_compiler = find_program('blueprint-compiler')
  blueprint_sources = files('window.blp', 'foo.blp', ...)
  blueprints = custom_target('blueprints',
    input:  blueprint_sources,
    output: '.',
    command: [blueprint_compiler, 'batch-compile',
              '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
  )
  gnome.compile_resources(
    APP_ID,
    APP_ID + '.gresource.xml',
    gresource_bundle: true,
    install: true,
    install_dir: pkgdatadir,
    dependencies: blueprints,
    source_dir: meson.current_build_dir(),
  )
  ```
- The Flatpak manifest must bundle blueprint-compiler so offline
  builds work. Cleanup `*` so it isn't shipped at runtime:
  ```json
  {
    "name": "blueprint-compiler",
    "buildsystem": "meson",
    "cleanup": ["*"],
    "sources": [{
      "type": "git",
      "url": "https://gitlab.gnome.org/jwestman/blueprint-compiler.git",
      "tag": "v0.16.0"
    }]
  }
  ```
- The gresource.xml lists files by their build-tree name (no `ui/`
  prefix) and aliases them under `/land/rob/<Project>/ui/...` so
  `Gtk.Template(resource_path='/land/rob/<Project>/ui/foo.ui')`
  works in Python.

## Flatpak

- Manifest at `build-aux/flatpak/land.rob.<Project>.json` (JSON, not
  YAML).
- Runtime: `org.gnome.Platform//50` + SDK. Bump in lockstep across
  projects when GNOME advances.
- Module order: blueprint-compiler first, then `python3-deps.json`,
  then the project module pointing at `"path": "../.."` (the source
  tree is the repo root, two levels up from the manifest).
- `python3-deps.json` is generated by `flatpak-pip-generator`,
  gitignored, regenerated when `requirements.txt` changes.
- Patch source tarballs to pre-built wheels with
  `fix-flatpak-deps.py build-aux/flatpak/python3-deps.json` so the
  build sandbox doesn't need a Rust toolchain for `cryptography`
  etc. The script is idempotent and emits multi-arch wheels gated by
  `only-arches` so a single manifest covers x86_64 + aarch64.

## Build driver

`build-all.sh` is a thin wrapper that:

- accepts `--arch <x86_64|aarch64>`, `--regen-deps`, `--install`
- runs `fix-flatpak-deps.py` (idempotent) before building
- warns if qemu binfmt isn't registered when cross-building
- emits `<project>-<arch>.flatpak` bundles at the project root
- does not push or sign anything

Keep it identical across projects so the user has muscle memory for
`./build-all.sh --arch aarch64 --install`.

## Async work

- One asyncio loop on a daemon thread (`async_loop.py`) shared by all
  network code. Marshal results back with `GLib.idle_add`. Don't sleep
  or block the GTK main loop. Don't spawn ad-hoc threads when the
  shared loop will do.

## Imports and Python conventions

- **Single-entry `gi.require_version`**: declare the required GI
  versions exactly once at the application entry point (the launcher
  `<project>.in` and/or `main.py`), before any `from gi.repository`
  import. Sub-modules just `from gi.repository import …` directly —
  no repeated `require_version` per file. Keeps the imports terse;
  any module that's run in isolation should pull in the entry-point
  module first.
  ```python
  # main.py / <project>.in (entry point, runs first)
  import gi
  gi.require_version('Gtk', '4.0')
  gi.require_version('Adw', '1')
  from gi.repository import Gtk, Adw, Gio
  ```
  ```python
  # any other module
  from gi.repository import Gtk, Adw, Gio, GLib
  ```
- Use `from __future__ import annotations` for projects targeting
  3.10+ when they use forward references.
- Type-hint internal APIs lightly; don't over-annotate one-shot
  helpers.
- Debug output is gated behind a `--debug` flag and a `debug` module
  (`debug.exception(msg, exc)`); never use bare `print()` in shipped
  code.

## Documentation

- **README.md** — public-facing. Disclosure that the code is written
  by Claude. Features, install, build, layout reminder, license
  pointer.
- **CLAUDE.md** — committed (no identifying info — no real
  hostnames, IPs, emails, paths with `/home/rob/`). Sections:
  *What this project is*, *Tech stack*, *Source layout*,
  *Build workflow*, *Key conventions*, *Things to watch out for*.
- **DESIGN.md** — optional architecture overview. The "why" of the
  project: pedagogy, stack, design decisions, state machine. Tonic
  and jamjar have one.
- **TODO.md** — optional backlog with rationale. One file per
  project; older `ROADMAP.md` / `BACKLOG.md` variants should fold
  into it. Banter and jamjar have one.
- **STYLE_GUIDE.md** — this file. Drop in unchanged.

## .gitignore

Use the curated short form, not the upstream Python boilerplate:

```
# Build artifacts
_build/
_flatpak/
_flatpak_x86_64/
_flatpak_aarch64/
.flatpak-builder/
repo/
*.flatpak

# Python
__pycache__/
*.pyc
*.egg-info/
.venv/

# Editors
.vscode/
.idea/
*.swp
.DS_Store

# Claude workspace
.claude/

# Generated (regenerated per build)
build-aux/flatpak/python3-deps.json
build-aux/flatpak/python3-deps.json.bak
```

`CLAUDE.md` is **tracked**, not gitignored. The `.claude/` directory
(Claude workspace state) is ignored in full — no carve-out for
`settings.json` or anything else. `CLAUDE.md` at the project root is
project documentation and ships with the repo.

## Phone install (postmarketOS / Phosh)

The default postmarketOS `nftables` ruleset has `policy drop` on
input with no allowance for mDNS or SSDP. Drop these in for any app
that does device discovery:

```
# /etc/nftables.d/60_mdns.nft
table inet filter {
    chain input {
        iifname "wlan*" udp dport 5353 accept comment "mDNS"
    }
}

# /etc/nftables.d/61_ssdp.nft
table inet filter {
    chain input {
        iifname "wlan*" udp sport 1900 accept comment "SSDP responses"
    }
}
```

Then `sudo nft -f /etc/nftables.nft`.
