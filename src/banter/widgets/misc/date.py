"""DateSeparator — centered pill label shown between messages on
different calendar days."""

from datetime import datetime

from gi.repository import Gtk


class DateSeparator(Gtk.Box):
    """Centered pill label shown between messages on different calendar days."""

    def __init__(self, d):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.add_css_class("date-separator")
        self.set_halign(Gtk.Align.CENTER)

        text = self._format(d)
        lbl  = Gtk.Label(label=text)
        lbl.add_css_class("date-separator-label")
        self.append(lbl)

    @staticmethod
    def _format(d) -> str:
        today     = datetime.now().date()
        yesterday = today.__class__.fromordinal(today.toordinal() - 1)
        if d == today:
            return "Today"
        if d == yesterday:
            return "Yesterday"
        # Within the current year: "Monday, January 6"
        if d.year == today.year:
            return d.strftime("%A, %B %-d")
        # Older: "January 6, 2023"
        return d.strftime("%B %-d, %Y")
