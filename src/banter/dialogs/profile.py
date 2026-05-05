"""Banter — EditProfileDialog: edit the signed-in user's GroupMe profile."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, GLib, Gdk

from ..async_utils import run_in_background
from ..helpers import set_avatar_from_url


class EditProfileDialog(Adw.PreferencesDialog):
    """Edit the signed-in user's GroupMe profile.

    Wraps `api.get_me` (load) + `api.upload_image` (avatar) +
    `api.update_me` (save). Update fields lazily — only changed
    values are POSTed, so a no-op Save is a single empty POST.

    `on_saved` is invoked with the fresh user dict after a
    successful save so BanterWindow can refresh `_current_user` and
    repaint the sidebar's account button tooltip.
    """

    def __init__(self, api, parent, on_saved=None):
        super().__init__()
        self.set_title("Edit Profile")
        self._api      = api
        self._parent   = parent
        self._on_saved = on_saved
        # Pending avatar URL — populated by the upload worker after
        # the user picks an image. Saved with the rest of the form.
        self._pending_image_url: str | None = None
        # Cached snapshot of the user dict at load time. Compared
        # field-by-field on save so we only send what's actually
        # changed.
        self._initial: dict = {}

        page = Adw.PreferencesPage()
        self.add(page)

        # ── Avatar ─────────────────────────────────────────────
        av_grp = Adw.PreferencesGroup()
        av_row = Adw.ActionRow(title="Avatar",
                                subtitle="Tap to choose a new image")
        av_row.set_activatable(True)
        self._avatar = Adw.Avatar(size=48, show_initials=True)
        av_row.add_prefix(self._avatar)
        av_row.add_suffix(
            Gtk.Image.new_from_icon_name("document-edit-symbolic"))
        av_row.connect("activated", self._pick_avatar)
        av_grp.add(av_row)
        page.add(av_grp)

        # ── Identity fields ────────────────────────────────────
        info_grp = Adw.PreferencesGroup(title="Profile")

        self._name_row = Adw.EntryRow(title="Name")
        info_grp.add(self._name_row)

        self._email_row = Adw.EntryRow(title="Email")
        info_grp.add(self._email_row)

        self._zip_row = Adw.EntryRow(title="ZIP Code")
        info_grp.add(self._zip_row)

        # GroupMe's bio field is short (~280 chars). EntryRow handles
        # that fine; multi-line would need a TextView and lose the
        # consistent EntryRow look-and-feel.
        self._bio_row = Adw.EntryRow(title="Bio")
        info_grp.add(self._bio_row)

        save_row = Adw.ButtonRow(title="Save Changes",
                                  start_icon_name="document-save-symbolic")
        save_row.add_css_class("suggested-action")
        save_row.connect("activated", self._save)
        info_grp.add(save_row)

        page.add(info_grp)

        # Kick off the load. _users/me already populates the cache
        # in BanterWindow.start, but re-fetching keeps the form in
        # sync if other clients have edited the profile in the
        # meantime.
        self._load()

    # ── Load ───────────────────────────────────────────────────
    def _load(self):
        api = self._api

        def worker():
            me = api.get_me() or {}
            GLib.idle_add(self._on_loaded, me)

        run_in_background(worker)

    def _on_loaded(self, me: dict):
        self._initial = dict(me or {})
        name = me.get("name") or ""
        self._name_row.set_text(name)
        self._email_row.set_text(me.get("email") or "")
        self._zip_row.set_text(me.get("zip_code") or "")
        self._bio_row.set_text(me.get("bio") or "")
        self._avatar.set_text(name)
        avatar_url = me.get("avatar_url") or me.get("image_url") or ""
        if avatar_url:
            set_avatar_from_url(self._avatar, avatar_url)

    # ── Avatar pick + upload ───────────────────────────────────
    def _pick_avatar(self, *_):
        fd = Gtk.FileDialog()
        fd.set_title("Choose Avatar")
        # Filter to just images so the picker doesn't surface every
        # file. PNG / JPEG / WebP / GIF cover what GroupMe accepts.
        flt = Gtk.FileFilter()
        flt.set_name("Images")
        for mime in ("image/png", "image/jpeg",
                     "image/webp", "image/gif"):
            flt.add_mime_type(mime)
        filters = Gio_list = Gtk.gio_list = None  # silence linter
        from gi.repository import Gio
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        fd.set_filters(store)
        fd.set_default_filter(flt)
        fd.open(self._parent, None, self._on_avatar_picked)

    def _on_avatar_picked(self, fd, result):
        try:
            f    = fd.open_finish(result)
            path = f.get_path()
        except GLib.Error:
            return

        try: self._parent.toast("Uploading avatar…")
        except Exception: pass

        api = self._api

        def worker():
            url = api.upload_image(path)
            GLib.idle_add(self._on_avatar_uploaded, url)

        run_in_background(worker)

    def _on_avatar_uploaded(self, url):
        if not url:
            try: self._parent.toast("Avatar upload failed")
            except Exception: pass
            return
        self._pending_image_url = url
        # Reflect the new image immediately so the user can see it
        # before they Save. set_avatar_from_url handles the network
        # fetch + scaling.
        set_avatar_from_url(self._avatar, url)

    # ── Save ───────────────────────────────────────────────────
    def _save(self, *_):
        # Build a sparse update dict — only fields that actually
        # changed against `_initial`. GroupMe's /users/update accepts
        # any subset of the editable fields, and an empty body is a
        # silent no-op.
        diff: dict = {}
        name = self._name_row.get_text().strip()
        if name and name != (self._initial.get("name") or ""):
            diff["name"] = name
        email = self._email_row.get_text().strip()
        if email != (self._initial.get("email") or ""):
            diff["email"] = email
        zip_code = self._zip_row.get_text().strip()
        if zip_code != (self._initial.get("zip_code") or ""):
            diff["zip_code"] = zip_code
        bio = self._bio_row.get_text().strip()
        if bio != (self._initial.get("bio") or ""):
            diff["bio"] = bio
        if self._pending_image_url:
            # The web client uses `avatar_url` here; older docs say
            # `image_url`. Send both — the server picks the one it
            # recognises and ignores the other.
            diff["avatar_url"] = self._pending_image_url
            diff["image_url"]  = self._pending_image_url

        if not diff:
            try: self._parent.toast("No changes")
            except Exception: pass
            self.close()
            return

        api = self._api

        def worker():
            r = api.update_me(**diff)
            GLib.idle_add(self._after_save, r)

        run_in_background(worker)

    def _after_save(self, r):
        try:
            self._parent.toast("Profile saved" if r else "Save failed")
        except Exception:
            pass
        if r and callable(self._on_saved):
            try: self._on_saved(r)
            except Exception: pass
        if r:
            self.close()
