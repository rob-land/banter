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
  'icons/hicolor/scalable/apps/land.rob.Banter.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'scalable' / 'apps',
)

install_data(
  'icons/hicolor/symbolic/apps/land.rob.Banter-symbolic.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)
