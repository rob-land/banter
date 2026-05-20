"""BanterWindow package.

The original `window.py` was a 1619-line single-class file; it's now
a package with feature mixins assembled into the templated
`BanterWindow` class. External callers still do
`from banter.window import BanterWindow` — this `__init__.py`
re-exports the class verbatim.
"""

from .window import BanterWindow

__all__ = ["BanterWindow"]
