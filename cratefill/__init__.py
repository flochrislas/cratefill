"""Cratefill — move songs between CSV files, music folders and YouTube Music.

Kept deliberately empty apart from the version: importing `cratefill` must stay
cheap (no Tkinter, no ytmusicapi), because setuptools reads __version__ from
here as the single source of truth for packaging. Import the pieces you need
from the submodules — `cratefill.app`, `cratefill.matching`,
`cratefill.storage`, `cratefill.youtube`.
"""

__version__ = "0.1.1"
