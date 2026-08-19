"""Local file handling: CSV import/export, music folders, and the data directory.

Pure and dependency-free — no Tkinter, no ytmusicapi — so every function here
is directly testable. See tests/test_storage.py.
"""

import csv
import os
import re
import sys
from pathlib import Path


def user_data_dir():
    """Per-user directory for browser.json, following each platform's convention.

    Deliberately *not* next to the script or the .exe: that location can be
    read-only (system-wide installs), gets wiped on every launch under
    PyInstaller --onefile, and invites copying the session cookies around
    together with the executable.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / "Cratefill"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cratefill"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "cratefill"


ARTIST_HEADERS = ("artist", "artiste", "interprete", "interprète")
TITLE_HEADERS = ("title", "titre", "song", "track", "chanson", "morceau", "name")
STATION_HEADERS = ("station", "radio", "chaine", "chaîne", "source")

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}


def read_songs_csv(path):
    """Return a list of (artist, title, station) tuples from a CSV file.

    Detects the delimiter, and finds the columns by header name; falls back
    to artist/title in the first two columns when headers are unrecognized.
    The station (where the user heard the song) is optional, only picked up
    from a recognized header, and "" when absent.
    """
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows = [row for row in csv.reader(text.splitlines(), dialect) if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = [cell.strip().casefold() for cell in rows[0]]
    artist_col = next((i for i, h in enumerate(header) if h in ARTIST_HEADERS), None)
    title_col = next((i for i, h in enumerate(header) if h in TITLE_HEADERS), None)
    station_col = next((i for i, h in enumerate(header) if h in STATION_HEADERS), None)
    if artist_col is not None and title_col is not None:
        rows = rows[1:]
    else:
        artist_col, title_col = 0, 1
        # Drop the first row anyway if it looks like a header we just couldn't map.
        if header and any(h in ARTIST_HEADERS + TITLE_HEADERS for h in header):
            rows = rows[1:]
        else:
            station_col = None  # no header row, so no way to spot a station column

    songs = []
    for row in rows:
        if len(row) <= max(artist_col, title_col):
            continue
        artist = row[artist_col].strip()
        title = row[title_col].strip()
        station = row[station_col].strip() if station_col is not None and station_col < len(row) else ""
        if artist or title:
            songs.append((artist, title, station))
    return songs


def read_songs_folder(path):
    """Return (artist, title, station) tuples from a folder of music files.

    The folder name fills the artist column and the file name (without
    extension) the title column, so the YouTube Music search query becomes
    "folder name + file name" — works best for folders named after an artist,
    an album, or a station whose files are "Artist - Title.ext".
    """
    folder = Path(path)
    artist = " ".join(folder.name.split())
    return [
        (artist, " ".join(f.stem.split()), "")
        for f in sorted(folder.iterdir())
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    ]


def safe_filename(name):
    """Turn a playlist title into a usable Windows/Linux file name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "playlist"


def write_playlist_csv(title, tracks, dest_dir):
    """Write one playlist to '<title>.csv' in dest_dir and return the Path.

    tracks is the "tracks" list of ytmusicapi's get_playlist(). Columns are
    Artist, Title, Album (album is often missing — then ""). Existing files
    are never overwritten; a " (2)", " (3)"… suffix is added instead.
    """
    base = safe_filename(title)
    path = Path(dest_dir) / f"{base}.csv"
    n = 2
    while path.exists():
        path = Path(dest_dir) / f"{base} ({n}).csv"
        n += 1
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Artist", "Title", "Album"])
        for t in tracks:
            artists = ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name"))
            album = (t.get("album") or {}).get("name", "")
            writer.writerow([artists, t.get("title", ""), album])
    return path
