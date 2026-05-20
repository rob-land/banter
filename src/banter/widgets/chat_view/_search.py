"""SearchMixin — in-conversation Ctrl+F search.

Mixed into ChatView. Plain in-text substring search over the loaded
bubbles; matches get a CSS class for highlighting and the next-match
buttons step through them in order.
"""

from ..message_bubble import MessageBubble


class SearchMixin:
    def toggle_search(self):
        """Show/hide the chat search bar. Called by BanterWindow's
        Ctrl+F action."""
        new_state = not self._search_bar.get_search_mode()
        self._search_bar.set_search_mode(new_state)
        if new_state:
            self._search_entry.grab_focus()
        else:
            self._clear_search_highlights()

    def _on_chat_search_changed(self, entry):
        query = entry.get_text().strip().lower()
        self._clear_search_highlights()
        self._search_matches = []
        if not query:
            self._search_status.set_text("")
            return
        # Walk the message box in order; collect bubbles whose text
        # contains the query, then highlight them.
        child = self._msgs_box.get_first_child()
        while child is not None:
            if isinstance(child, MessageBubble):
                text = (child.msg.get("text") or "").lower()
                if query in text:
                    child.add_css_class("search-match")
                    self._search_matches.append(child)
            child = child.get_next_sibling()
        if not self._search_matches:
            self._search_status.set_text("No matches")
            return
        # Jump to the most recent (last) match by default — matches the
        # convention where the user is usually looking for something
        # they recently saw.
        self._search_index = len(self._search_matches) - 1
        self._update_search_status()
        self._scroll_to_bubble(self._search_matches[self._search_index])

    def _chat_search_step(self, direction: int):
        if not self._search_matches:
            return
        n = len(self._search_matches)
        self._search_index = (self._search_index + direction) % n
        self._update_search_status()
        self._scroll_to_bubble(self._search_matches[self._search_index])

    def _update_search_status(self):
        n = len(self._search_matches)
        if n == 0:
            self._search_status.set_text("")
        else:
            self._search_status.set_text(
                f"{self._search_index + 1} of {n}")

    def _clear_search_highlights(self):
        for b in self._search_matches:
            try:
                b.remove_css_class("search-match")
            except Exception:
                pass
        self._search_matches = []
        self._search_index   = 0
        self._search_status.set_text("")
