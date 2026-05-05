"""Banter — MentionPopover: @-mention autocomplete for the compose bar."""

from gi.repository import Gtk, GObject


# Sentinel used as the user_id for the synthetic "@everyone" entry.
# ChatView resolves this to the full member list when building the
# mentions attachment at send time (GroupMe has no real broadcast id —
# the official client just packs every member's user_id into a single
# mentions attachment with the same loci range repeated).
EVERYONE_ID = "__everyone__"


class MentionPopover(Gtk.Popover):
    """Filterable autocomplete popover for @-mentions.

    Emits ``member-selected`` with (display_name, user_id) when the user
    activates a row (Enter, Tab, click). user_id is ``EVERYONE_ID`` for
    the @everyone synthetic entry.
    """

    __gsignals__ = {
        "member-selected": (
            GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, members):
        """members: iterable of (display_name, user_id) tuples."""
        super().__init__()
        self.set_position(Gtk.PositionType.TOP)
        # autohide=False keeps focus in the TextView while the popover
        # is up; we handle dismissal explicitly from ChatView.
        self.set_autohide(False)
        self.set_has_arrow(True)
        self._members = sorted(members, key=lambda m: m[0].lower())

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_width(240)
        # propagate_natural_height=True lets the SW grow to fit the
        # rows up to max_content_height, so we show ~5 rows by default
        # (and fewer when the filtered list is shorter) instead of
        # collapsing to one-row minimum.
        scroll.set_propagate_natural_height(True)
        scroll.set_min_content_height(40)
        scroll.set_max_content_height(220)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.add_css_class("navigation-sidebar")
        self._listbox.connect("row-activated", self._on_row_activated)
        scroll.set_child(self._listbox)
        self.set_child(scroll)

        self.set_filter("")

    def set_filter(self, prefix: str):
        """Re-populate the list with members matching `prefix` (case-insensitive)."""
        prefix_low = prefix.lower()
        # Clear existing rows
        while True:
            row = self._listbox.get_first_child()
            if row is None:
                break
            self._listbox.remove(row)

        if not prefix_low or "everyone".startswith(prefix_low):
            self._add_row("everyone", EVERYONE_ID)
        for name, user_id in self._members:
            n_low = name.lower()
            if (not prefix_low
                    or n_low.startswith(prefix_low)
                    or prefix_low in n_low):
                self._add_row(name, user_id)

        first = self._listbox.get_row_at_index(0)
        if first is not None:
            self._listbox.select_row(first)

    def _add_row(self, display_name: str, user_id: str):
        row = Gtk.ListBoxRow()
        lbl = Gtk.Label(label=display_name, xalign=0)
        lbl.set_margin_start(10)
        lbl.set_margin_end(10)
        lbl.set_margin_top(4)
        lbl.set_margin_bottom(4)
        row.set_child(lbl)
        # Stash the data on the row so _on_row_activated can read it back.
        row.user_id = user_id
        row.display_name = display_name
        self._listbox.append(row)

    def _on_row_activated(self, _box, row):
        self.emit("member-selected", row.display_name, row.user_id)

    def navigate(self, direction: int):
        """Move the selection by `direction` (+1 down, -1 up). Cycles."""
        n = 0
        while self._listbox.get_row_at_index(n):
            n += 1
        if n == 0:
            return
        cur = self._listbox.get_selected_row()
        idx = cur.get_index() if cur else -1
        new_idx = (idx + direction) % n
        self._listbox.select_row(self._listbox.get_row_at_index(new_idx))

    def accept(self) -> bool:
        """Activate the currently-selected row. Returns False if no row."""
        row = self._listbox.get_selected_row()
        if row is None:
            return False
        self.emit("member-selected", row.display_name, row.user_id)
        return True

    def has_results(self) -> bool:
        return self._listbox.get_row_at_index(0) is not None
