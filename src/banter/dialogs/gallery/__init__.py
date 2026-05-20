"""Banter — gallery + album dialogs.

The original `dialogs/gallery.py` accumulated four distinct dialog
classes plus a handful of helpers. They're split across submodules
now; this package re-exports the public names so existing
`from ..dialogs.gallery import …` imports keep working.
"""

from ._helpers import add_local_image_to_album
from .album_creator import AlbumCreatorDialog
from .album_picker import AlbumPickerDialog
from .album_view import AlbumViewDialog
from .gallery import GalleryDialog

__all__ = [
    'AlbumCreatorDialog',
    'AlbumPickerDialog',
    'AlbumViewDialog',
    'GalleryDialog',
    'add_local_image_to_album',
]
