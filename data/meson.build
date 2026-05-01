i18n    = import('i18n')
datadir = get_option('prefix') / get_option('datadir')

desktop_file = i18n.merge_file(
  input:       'land.rob.Banter.desktop.in',
  output:      'land.rob.Banter.desktop',
  type:        'desktop',
  po_dir:      '../po',
  install:     true,
  install_dir: datadir / 'applications',
)

appdata_file = i18n.merge_file(
  input:       'land.rob.Banter.appdata.xml.in',
  output:      'land.rob.Banter.appdata.xml',
  po_dir:      '../po',
  install:     true,
  install_dir: datadir / 'metainfo',
)

install_data(
  'land.rob.Banter.gschema.xml',
  install_dir: datadir / 'glib-2.0' / 'schemas',
)

install_data(
  'banter-demo.json',
  install_dir: datadir / 'banter',
)

install_data(
  'icons/hicolor/scalable/apps/land.rob.Banter.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'scalable' / 'apps',
)

install_data(
  'icons/hicolor/symbolic/apps/land.rob.Banter-symbolic.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)

# Bundled fallback bell icon — Adwaita ships notifications-disabled-symbolic
# but no plain unmuted variant, so we need to provide our own for the
# mute toggle's "not muted" state. Lives under scalable/actions because
# hicolor's index.theme registers that context dir but not symbolic/actions
# (symbolic SVGs in hicolor go in scalable/, distinguished by the
# `-symbolic.svg` filename suffix).
install_data(
  'icons/hicolor/scalable/actions/banter-notifications-active-symbolic.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'scalable' / 'actions',
)
