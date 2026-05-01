project('banter',
  version: '0.1.0',
  meson_version: '>= 0.62.0',
  default_options: ['warning_level=2'],
)

gnome  = import('gnome')
python = import('python').find_installation('python3')

prefix     = get_option('prefix')
bindir     = prefix / get_option('bindir')
datadir    = prefix / get_option('datadir')
pkgdatadir = datadir / meson.project_name()
moduledir  = pkgdatadir / 'banter'

subdir('data')
subdir('po')
subdir('src')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
