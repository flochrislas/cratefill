"""Everything that talks to YouTube Music, plus the credentials it needs.

No Tkinter here. The functions that report progress take a `put` callable —
in the app that is `worker_queue.put`, in tests a list's append — and emit the
same (kind, payload) tuples the UI drains in CratefillApp._poll_worker:

    ("log", text)        a line for the Messages pane
    ("step", None)       advance the progress bar one step
    ("playlists", list)  a freshly fetched library
    ("account", text)    new text for the account label

Credentials live here rather than in storage.py: they belong to authentication
rather than to general application data. Only the *location* of the per-user
data directory is storage's business (storage.user_data_dir).
"""

import os
import re
import sys
import tempfile
from pathlib import Path

import ytmusicapi
from ytmusicapi import YTMusic

from .matching import pick_match
from .storage import user_data_dir, write_playlist_csv

AUTH_FILE = user_data_dir() / "browser.json"
# Where browser.json used to live in earlier versions; migrated away on startup.
# Two .parent hops: this file sits in the package directory, and the legacy file
# sat next to the old single-module cratefill.py one level up (the repo root for
# a checkout, site-packages for an install). Frozen builds keep using the
# executable's own directory.
LEGACY_AUTH_FILE = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
) / "browser.json"


def secure_auth_dir():
    """Create the data directory, owner-only on POSIX."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            AUTH_FILE.parent.chmod(0o700)
        except OSError:
            pass  # best effort; the file mode below is what actually matters


def secure_auth_file(path=None):
    """Restrict a credentials file to the owner — ytmusicapi writes it with the
    process umask, which commonly leaves it world-readable."""
    path = AUTH_FILE if path is None else Path(path)
    if os.name == "posix" and path.exists():
        try:
            path.chmod(0o600)
        except OSError:
            pass


def migrate_legacy_auth_file():
    """Move an older browser.json into the user data dir, so upgrading
    doesn't force a re-login and no readable copy is left behind."""
    if LEGACY_AUTH_FILE == AUTH_FILE or not LEGACY_AUTH_FILE.exists():
        return False
    try:
        secure_auth_dir()
        if not AUTH_FILE.exists():
            # Copy through os.open rather than replace()/shutil.move: the old and
            # new locations are often on different filesystems, and this way the
            # new file is never momentarily readable by anyone else.
            data = LEGACY_AUTH_FILE.read_bytes()
            fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        LEGACY_AUTH_FILE.unlink()  # leave no readable copy behind
        secure_auth_file()
        return True
    except OSError:
        return False  # unwritable data dir: leave the old file alone, user re-logs in


# Plausible header name, optionally with the ":" prefix of HTTP/2 pseudo-headers
# (":authority") or the trailing ":" Chrome sometimes keeps on name lines.
HEADER_NAME_RE = re.compile(r":?[A-Za-z][A-Za-z0-9_-]*:?")


def clean_pasted_headers(raw):
    """Rebuild a {name: value} dict from request headers pasted out of DevTools.

    Accepts both the one-line format ("name: value", e.g. Firefox's Copy
    Request Headers) and the Chrome/Edge headers-panel selection, where names
    and values land on alternating lines. HTTP/2 pseudo-headers (":authority"
    etc.) and the decoded x-client-data protobuf block are dropped: fed
    straight to ytmusicapi they desync its parser into writing bogus headers
    (e.g. a request path as a header name) that make YouTube reject every
    request with a non-JSON error.
    """
    headers = {}
    pending = None  # header name waiting for its value on the next line
    in_decoded = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if in_decoded:
            in_decoded = line != "}"
        elif pending is not None:
            if not pending.startswith(":"):
                headers[pending] = line
            pending = None
        elif line.startswith("Decoded:"):
            in_decoded = True
        else:
            name, sep, value = line.partition(":")
            if sep and value.strip() and HEADER_NAME_RE.fullmatch(name):
                headers[name.lower()] = value.strip()
            elif HEADER_NAME_RE.fullmatch(line):
                # Name alone on its line; pseudo-header names keep their ":"
                # so the pair is consumed but not stored. Anything else
                # (request line, protobuf leftovers) is ignored.
                pending = line.rstrip(":").lower()
    return headers


def open_session():
    """Build a YTMusic client from the saved session. Raises if unusable."""
    return YTMusic(str(AUTH_FILE))


def save_credentials(headers):
    """Write, validate and install new browser-auth credentials.

    Stages the new credentials in a sibling file and only swaps them in once
    they are known to work, so a mistyped re-login leaves the existing session
    untouched. Same directory means same filesystem, which is what makes
    os.replace atomic. Raises on failure, having removed the staged file.
    """
    secure_auth_dir()
    fd, tmp_name = tempfile.mkstemp(
        dir=AUTH_FILE.parent, prefix=".browser-", suffix=".json"
    )
    os.close(fd)  # mkstemp created it 0600; setup() rewrites it in place
    staged = Path(tmp_name)
    try:
        ytmusicapi.setup(
            filepath=str(staged),
            headers_raw="\n".join(f"{k}: {v}" for k, v in headers.items()),
        )
        secure_auth_file(staged)  # ytmusicapi writes with the process umask
        YTMusic(str(staged)).get_library_playlists(limit=1)  # validate
        os.replace(staged, AUTH_FILE)  # atomic: never a half-written session
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def fetch_playlists(yt, put):
    """Fetch the user's playlists and hand them to the UI.

    Swallows its own errors so callers can use it as a final step without
    losing whatever they already reported.
    """
    try:
        put(("playlists", yt.get_library_playlists(limit=None)))
    except Exception as e:
        put(("log", f"Could not fetch playlists: {e}"))
        put(("account", "Login expired? Re-log in."))


def add_songs_to_playlists(yt, songs, playlists, put):
    """Search every song, then add the matches to each playlist.

    `yt` is the client captured when the job started, so the job stays bound to
    one account even if the user logs into another one afterwards.
    """
    video_ids = []
    not_found = 0
    for artist, title, _station in songs:  # station is context for the user, not a search term
        query = f"{artist} {title}".strip()
        try:
            results = yt.search(query, filter="songs", limit=5)
        except Exception as e:
            put(("log", f"✗ {artist} — {title}: search failed ({e})"))
            put(("step", None))
            not_found += 1
            continue
        match, confident = pick_match(results, artist, title)
        if match is None:
            put(("log", f"✗ {artist} — {title}: no match found"))
            not_found += 1
        else:
            video_ids.append(match["videoId"])
            found_artists = ", ".join(
                a.get("name") or ""
                for a in (match.get("artists") or [])
                if isinstance(a, dict)
            )
            if confident:
                put(("log", f"✓ {artist} — {title}"))
            else:
                put(("log", f"? {artist} — {title}: uncertain match "
                            f"→ {found_artists} — {match.get('title')}"))
        put(("step", None))

    video_ids = list(dict.fromkeys(video_ids))  # two rows can match the same YT song

    def status_of(result):
        return str(result.get("status", "")) if isinstance(result, dict) else str(result)

    for pl in playlists:
        if not video_ids:
            put(("step", None))
            continue
        try:
            # YT Music rejects the whole batch if even one song is already in
            # the playlist (no items get added), so on failure drop the songs
            # it already contains and retry with the rest.
            to_add = video_ids
            skipped = 0
            result = yt.add_playlist_items(pl["playlistId"], to_add, duplicates=False)
            if "SUCCEEDED" not in status_of(result):
                existing = {
                    t.get("videoId")
                    for t in yt.get_playlist(pl["playlistId"], limit=None).get("tracks", [])
                }
                to_add = [v for v in video_ids if v not in existing]
                skipped = len(video_ids) - len(to_add)
                if not to_add:
                    put(("log", f"→ '{pl['title']}': all {len(video_ids)} song(s) "
                                "are already in the playlist — nothing to add"))
                    put(("step", None))
                    continue
                result = yt.add_playlist_items(pl["playlistId"], to_add, duplicates=False)
            status = status_of(result)
            if "SUCCEEDED" in status:
                message = f"→ Added {len(to_add)} song(s) to '{pl['title']}'"
                if skipped:
                    message += f" ({skipped} already there, skipped)"
                put(("log", message))
            else:
                put(("log", f"→ '{pl['title']}': {status} (playlist not editable, or YT Music "
                            "sees some of these songs as duplicates under different ids)"))
        except Exception as e:
            put(("log", f"→ Failed to add to '{pl['title']}': {e}"))
        put(("step", None))

    summary = f"--- Done. {len(video_ids)} matched, {not_found} not found. ---"
    put(("log", summary))


def export_playlists_to_csv(yt, playlists, dest, put):
    """Fetch each playlist's tracks and write a CSV per playlist."""
    for pl in playlists:
        try:
            tracks = yt.get_playlist(pl["playlistId"], limit=None).get("tracks", [])
            path = write_playlist_csv(pl["title"], tracks, dest)
            put(("log", f"→ Saved '{pl['title']}' ({len(tracks)} tracks) to {path.name}"))
        except Exception as e:
            put(("log", f"→ Failed to export '{pl['title']}': {e}"))
        put(("step", None))
    put(("log", "--- Export done. ---"))
