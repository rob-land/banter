blueprint_compiler = find_program('blueprint-compiler')

blueprint_sources = files(
  'help-overlay.blp',
  'jump-to-date-dialog.blp',
)

blueprints = custom_target('blueprints',
  input:  blueprint_sources,
  output: '.',
  command: [blueprint_compiler, 'batch-compile', '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
)

# style.css is checked in (not generated), so the GResource compile
# needs both the source dir (for style.css) and the build dir (for
# the generated *.ui files).
gresource = gnome.compile_resources(
  'land.rob.Banter',
  'land.rob.Banter.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: get_option('datadir') / 'banter',
  dependencies: blueprints,
  source_dir: [meson.current_source_dir(), meson.current_build_dir()],
)
