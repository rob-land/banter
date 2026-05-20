"""MessageBubble widget package.

The original `message_bubble.py` packed module helpers, the
EditMessageDialog, and a ~1360-line MessageBubble class into one
file. Now it's a package: stateless helpers and the dialog have
their own files, and MessageBubble's feature areas are split into
mixin modules. The public class is assembled here.
"""

from .bubble import MessageBubble
from .edit_dialog import EditMessageDialog

__all__ = ["MessageBubble", "EditMessageDialog"]
