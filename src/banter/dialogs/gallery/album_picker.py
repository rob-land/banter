"""AlbumPickerDialog — pick a destination album for a media set."""

from gi.repository import Adw, GLib, Gtk

from ...async_utils import run_in_background
from ...constants import esc
from ...widgets.base import StandardDialog
from ._helpers import _add_media
from .album_creator import AlbumCreatorDialog


class AlbumPickerDialog(StandardDialog):
    """Pick a destination album for a set of media items, or create a
    new one. The flow:

      1. Lists existing albums via api.get_albums.
      2. Tap an album → POST those items via api.add_to_album.
      3. Tap "New album…" → opens AlbumCreatorDialog; when that
         dialog reports a successfully-created album, the picker's
         add_to_album follow-up runs automatically.

    `on_done(ok: bool)` fires once after a successful add so the
    caller can clear selection state."""

    def __init__(self, api, group, media: list, parent, on_done=None):
        super().__init__(title="Add to Album", width=420, height=520)
        self._api = api
        self._group = group
        self._media = media
        self._parent = parent
        self._on_done = on_done

        body = self.set_scrolled_body(margin=12, spacing=12)

        n = len(media)
        hint = Gtk.Label(
            label=f"Adding {n} item{'s' if n != 1 else ''} to:",
            xalign=0)
        hint.add_css_class("dim-label")
        body.append(hint)

        self._spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        self._spinner.set_margin_top(20)
        body.append(self._spinner)

        self._list_grp = Adw.PreferencesGroup()
        self._list_grp.set_visible(False)
        body.append(self._list_grp)

        new_grp = Adw.PreferencesGroup()
        new_row = Adw.ButtonRow(title="New album…",
                                 start_icon_name="folder-new-symbolic")
        new_row.connect("activated", self._on_new_album)
        new_grp.add(new_row)
        body.append(new_grp)

        self._load_albums()

    def _load_albums(self):
        gid = self._group["id"]
        api = self._api

        def worker():
            albums = api.get_albums(gid)
            GLib.idle_add(self._populate, albums or [])
        run_in_background(worker)

    def _populate(self, albums: list):
        self._spinner.set_spinning(False)
        self._spinner.set_visible(False)
        self._list_grp.set_visible(True)

        if not albums:
            empty = Adw.ActionRow(title="No albums yet",
                                   subtitle="Use \"New album…\" below to create one.")
            empty.set_activatable(False)
            self._list_grp.add(empty)
            return

        for a in albums:
            row = Adw.ActionRow(title=esc(a.get("title", "Album")))
            imgs = int(a.get("total_images") or 0)
            videos = int(a.get("total_videos") or 0)
            counts = []
            if imgs:
                counts.append(f"{imgs} image" + ("s" if imgs != 1 else ""))
            if videos:
                counts.append(f"{videos} video" + ("s" if videos != 1 else ""))
            row.set_subtitle(" · ".join(counts) if counts else "Empty")
            row.set_activatable(True)
            row.connect("activated", self._add_to_existing, a)
            self._list_grp.add(row)

    def _add_to_existing(self, _row, album: dict):
        self._do_add(album)

    def _on_new_album(self, _row):
        # Hand off to the creator dialog with a follow-up that reuses
        # _do_add against the freshly-created album. Closing the
        # picker first avoids stacking three dialogs.
        media = self._media
        on_done = self._on_done
        api = self._api
        group = self._group
        parent = self._parent

        def after_create(album):
            _add_media(api, group, album, media, parent, on_done)

        self.close()
        AlbumCreatorDialog(
            api, group, parent, on_created=after_create
        ).present(parent)

    def _do_add(self, album: dict):
        _add_media(self._api, self._group, album, self._media,
                   self._parent, self._on_done)
        self.close()
