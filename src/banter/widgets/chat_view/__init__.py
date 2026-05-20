"""ChatView widget package.

The original `chat_view.py` was a 1995-line single-class file. It's
now a package: feature areas live in mixin modules and the public
class is assembled in `chat_view.py` from the mixins. External
callers still `from .chat_view import ChatView`; the import path is
preserved by this re-export.
"""

from .chat_view import ChatView

__all__ = ["ChatView"]
