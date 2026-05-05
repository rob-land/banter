"""Banter — shared widget bases.

`StandardDialog` consolidates the "Adw.Dialog + Adw.ToolbarView +
Adw.HeaderBar + Gtk.ScrolledWindow + margined Gtk.Box" pattern that
was previously copy-pasted across ~20 dialog classes. It keeps the
pieces explicit (callers can grab the inner Box and append rows) but
removes the boilerplate.
"""

from gi.repository import Gtk, Adw


class StandardDialog(Adw.Dialog):
    """Adw.Dialog with a header bar and a content slot.

    Typical use:

        class FooDialog(StandardDialog):
            def __init__(self, ...):
                super().__init__("Foo", width=420, height=560)
                body = self.set_scrolled_body(margin=12, spacing=16)
                body.append(...)

    For non-scrolled content use ``set_body(widget)`` instead. To add
    buttons / menus to the header, ``add_header_widget(w, end=True)``.
    """

    def __init__(self, title: str = "", width: int = 400, height: int = 560):
        super().__init__()
        if title:
            self.set_title(title)
        self.set_content_width(width)
        self.set_content_height(height)

        self._toolbar = Adw.ToolbarView()
        self._header  = Adw.HeaderBar()
        self._toolbar.add_top_bar(self._header)
        self.set_child(self._toolbar)

    # ── Public surface ──────────────────────────────────────────────
    def add_header_widget(self, widget: Gtk.Widget, end: bool = False):
        """Pack a widget into the header bar. `end=True` for the right side."""
        if end:
            self._header.pack_end(widget)
        else:
            self._header.pack_start(widget)

    def set_body(self, widget: Gtk.Widget):
        """Set an arbitrary widget as the dialog content."""
        self._toolbar.set_content(widget)

    def set_scrolled_body(self, *, margin: int = 16,
                          spacing: int = 12) -> Gtk.Box:
        """Replace the content with a vertical scrollable Box.

        Returns the inner Box so the caller can append children. The
        scroll container is created once per call; subsequent calls
        replace the previous body.
        """
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_kinetic_scrolling(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        body.set_margin_start(margin)
        body.set_margin_end(margin)
        body.set_margin_top(margin)
        body.set_margin_bottom(margin)

        scroll.set_child(body)
        self._toolbar.set_content(scroll)
        return body

    def add_bottom_bar(self, widget: Gtk.Widget):
        """Add a sticky bottom bar (e.g. nav strip, action toolbar)."""
        self._toolbar.add_bottom_bar(widget)
