"""AlbumCreatorDialog — name + create a fresh album."""

from gi.repository import Adw, GLib, Gtk

from ...async_utils import run_in_background
from ...widgets.base import StandardDialog


class AlbumCreatorDialog(StandardDialog):
    """Create a new album in a group's gallery.

    Backed by POST /v3/conversations/{gid}/albums/create. The album
    starts empty; the server auto-fills `cover_image_url` from the
    first media item added afterwards. Adding media is its own flow
    (see api.add_to_album); this dialog only handles creation.

    Pass `on_created` to chain a follow-up: the callback receives the
    new album dict on success and runs after the dialog closes."""

    def __init__(self, api, group, parent, on_created=None):
        super().__init__(title="New Album", width=380, height=-1)
        self._api = api
        self._group = group
        self._parent = parent
        self._on_created = on_created

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        self.add_header_widget(cancel_btn, end=False)

        self._create_btn = Gtk.Button(label="Create")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.connect("clicked", self._create)
        self.add_header_widget(self._create_btn, end=True)

        box = self.set_scrolled_body(margin=16, spacing=16)

        grp = Adw.PreferencesGroup()
        grp.set_description(
            "Albums collect images and videos from this group's "
            "gallery into a named set.")
        self._name_row = Adw.EntryRow(title="Album name")
        grp.add(self._name_row)
        box.append(grp)

    def _create(self, *_):
        title = self._name_row.get_text().strip()
        if not title:
            self._parent.toast("Album name is required")
            return

        self._create_btn.set_sensitive(False)
        self._create_btn.set_label("Creating…")
        gid = self._group["id"]
        api = self._api

        def worker():
            r = api.create_album(gid, title)
            GLib.idle_add(self._on_done, r, title)
        run_in_background(worker)

    def _on_done(self, album, title):
        self._create_btn.set_sensitive(True)
        self._create_btn.set_label("Create")
        if album:
            self._parent.toast(f"Album '{title}' created")
            self.close()
            if callable(self._on_created):
                self._on_created(album)
        else:
            self._parent.toast("Couldn't create album")
