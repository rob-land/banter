configure_file(
  input:        'banter.in',
  output:       'banter',
  configuration: conf,
  install:      true,
  install_dir:  bindir,
  install_mode: 'rwxr-xr-x',
)

const_py = configure_file(
  input:         'const.py.in',
  output:        'const.py',
  configuration: py_conf,
)

banter_sources = files(
  '__init__.py',
  'application.py',
  'api.py',
  'async_utils.py',
  'background_portal.py',
  'config.py',
  'constants.py',
  'helpers.py',
  'logging_setup.py',
  'main.py',
  'mock_api.py',
  'notifications.py',
  'notifier.py',
  'oauth.py',
  'push.py',
  'secrets.py',
  'window.py',
)

widget_sources = files(
  'widgets/__init__.py',
  'widgets/album_card.py',
  'widgets/base.py',
  'widgets/chat_view.py',
  'widgets/conversation_row.py',
  'widgets/event_card.py',
  'widgets/mention_popover.py',
  'widgets/message_bubble.py',
  'widgets/misc.py',
  'widgets/poll_card.py',
  'widgets/reactions_sheet.py',
)

dialog_sources = files(
  'dialogs/__init__.py',
  'dialogs/accounts.py',
  'dialogs/events.py',
  'dialogs/gallery.py',
  'dialogs/group.py',
  'dialogs/jump_to_date.py',
  'dialogs/members.py',
  'dialogs/pinned.py',
  'dialogs/preferences.py',
  'dialogs/profile.py',
  'dialogs/settings.py',
)

python.install_sources(banter_sources + [const_py], subdir: 'banter')
python.install_sources(widget_sources,  subdir: 'banter/widgets')
python.install_sources(dialog_sources,  subdir: 'banter/dialogs')
