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
)

window_sources = files(
  'window/__init__.py',
  'window/_actions.py',
  'window/_bg_poll.py',
  'window/_chat.py',
  'window/_login.py',
  'window/_mute.py',
  'window/_push.py',
  'window/_sidebar.py',
  'window/window.py',
)

widget_sources = files(
  'widgets/__init__.py',
  'widgets/album_card.py',
  'widgets/base.py',
  'widgets/conversation_row.py',
  'widgets/event_card.py',
  'widgets/mention_popover.py',
  'widgets/misc.py',
  'widgets/poll_card.py',
  'widgets/reactions_sheet.py',
)

chat_view_sources = files(
  'widgets/chat_view/__init__.py',
  'widgets/chat_view/_attachments.py',
  'widgets/chat_view/_jump_to_date.py',
  'widgets/chat_view/_mentions.py',
  'widgets/chat_view/_messages.py',
  'widgets/chat_view/_pinned.py',
  'widgets/chat_view/_polling.py',
  'widgets/chat_view/_read_receipts.py',
  'widgets/chat_view/_search.py',
  'widgets/chat_view/_sending.py',
  'widgets/chat_view/_typing.py',
  'widgets/chat_view/_voice.py',
  'widgets/chat_view/chat_view.py',
)

message_bubble_sources = files(
  'widgets/message_bubble/__init__.py',
  'widgets/message_bubble/_context_menu.py',
  'widgets/message_bubble/_reactions.py',
  'widgets/message_bubble/_save_actions.py',
  'widgets/message_bubble/_status.py',
  'widgets/message_bubble/_text.py',
  'widgets/message_bubble/bubble.py',
  'widgets/message_bubble/edit_dialog.py',
)

dialog_sources = files(
  'dialogs/__init__.py',
  'dialogs/accounts.py',
  'dialogs/events.py',
  'dialogs/group.py',
  'dialogs/jump_to_date.py',
  'dialogs/members.py',
  'dialogs/pinned.py',
  'dialogs/preferences.py',
  'dialogs/profile.py',
  'dialogs/settings.py',
)

gallery_sources = files(
  'dialogs/gallery/__init__.py',
  'dialogs/gallery/_helpers.py',
  'dialogs/gallery/album_creator.py',
  'dialogs/gallery/album_picker.py',
  'dialogs/gallery/album_view.py',
  'dialogs/gallery/gallery.py',
)

api_sources = files(
  'api/__init__.py',
  'api/_core.py',
  'api/dms.py',
  'api/extras.py',
  'api/groups.py',
  'api/media.py',
)

python.install_sources(banter_sources + [const_py],   subdir: 'banter')
python.install_sources(window_sources,          subdir: 'banter/window')
python.install_sources(api_sources,             subdir: 'banter/api')
python.install_sources(widget_sources,          subdir: 'banter/widgets')
python.install_sources(chat_view_sources,       subdir: 'banter/widgets/chat_view')
python.install_sources(message_bubble_sources,  subdir: 'banter/widgets/message_bubble')
python.install_sources(dialog_sources,          subdir: 'banter/dialogs')
python.install_sources(gallery_sources,         subdir: 'banter/dialogs/gallery')
