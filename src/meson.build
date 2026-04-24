pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()
moduledir  = pkgdatadir / 'banter'

configure_file(
  input:  'banter.in',
  output: 'banter',
  configuration: {
    'PYTHON'     : python.full_path(),
    'VERSION'    : meson.project_version(),
    'pkgdatadir' : pkgdatadir,
    'moduledir'  : moduledir,
  },
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

banter_sources = files(
  '__init__.py',
  'application.py',
  'api.py',
  'async_utils.py',
  'config.py',
  'constants.py',
  'css.py',
  'helpers.py',
  'main.py',
  'oauth.py',
  'push.py',
  'window.py',
)

widget_sources = files(
  'widgets/__init__.py',
  'widgets/chat_view.py',
  'widgets/conversation_row.py',
  'widgets/event_card.py',
  'widgets/message_bubble.py',
  'widgets/misc.py',
  'widgets/reactions_sheet.py',
)

dialog_sources = files(
  'dialogs/__init__.py',
  'dialogs/accounts.py',
  'dialogs/events.py',
  'dialogs/gallery.py',
  'dialogs/group.py',
  'dialogs/login.py',
  'dialogs/members.py',
  'dialogs/settings.py',
)

python.install_sources(banter_sources,  subdir: 'banter')
python.install_sources(widget_sources,  subdir: 'banter/widgets')
python.install_sources(dialog_sources,  subdir: 'banter/dialogs')
