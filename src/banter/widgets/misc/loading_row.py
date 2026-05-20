"""LoadingRow — Adw.ActionRow with a spinner suffix."""

from gi.repository import Adw, Gtk


class LoadingRow(Adw.ActionRow):
    def __init__(self, label="Loading…"):
        super().__init__(title=label)
        spinner = Gtk.Spinner(spinning=True)
        self.add_suffix(spinner)
        self.set_activatable(False)
