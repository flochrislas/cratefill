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
from dataclasses import dataclass, field
from pathlib import Path

import ytmusicapi
from ytmusicapi import YTMusic

from .matching import SEARCH_LIMIT, MatchDecision, choose_match, validate_request
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


def search_candidates(yt, artist, title):
    """Ask YouTube Music for candidates for one song.

    Several, not one: the results are evaluated on their merits rather than
    trusting whatever came back first.
    """
    query = f"{artist} {title}".strip()
    return yt.search(query, filter="songs", limit=SEARCH_LIMIT)


def evaluate_songs(yt, songs, put) -> list[tuple]:
    """Search and score every song. Returns [(song, MatchDecision), …].

    Phase one of adding: this function performs **no** mutating call, so the user
    can still cancel after seeing what would happen. `yt` is the client captured
    when the job started, so the job stays bound to one account even if the user
    logs into another one afterwards.

    A row repeated in the import reuses the first search for the exact same
    artist and title — it still gets its own decision, so it is reviewed on its
    own. Failed searches aren't cached.
    """
    evaluated, searched = [], {}
    for song in songs:
        artist, title = song[0], song[1]  # song[2] is the station: context, not a search term
        blocking = validate_request(artist, title)
        if blocking:
            # Nothing to search for — don't spend a network call on it.
            decision = MatchDecision("rejected", reasons=blocking)
            line = _decision_line(artist, title, decision)
        else:
            try:
                if (artist, title) not in searched:
                    searched[(artist, title)] = search_candidates(yt, artist, title)
            except Exception as e:
                # Not "no match": nothing was learnt about this song at all.
                decision = MatchDecision("rejected", reasons=[f"search failed ({e})"])
                line = f"✗ {artist} — {title}: search failed — {e}"
            else:
                decision = choose_match(artist, title, searched[(artist, title)])
                line = _decision_line(artist, title, decision)
        evaluated.append((song, decision))
        put(("log", line))
        put(("step", None))
    return evaluated


def _decision_line(artist, title, decision):
    """One Messages-pane line describing what matching concluded."""
    if decision.status == "high":
        return f"✓ {artist} — {title}"
    if decision.status in ("ambiguous", "weak"):
        qualifier = "uncertain" if decision.status == "ambiguous" else "weak match, will ask"
        return (f"? {artist} — {title}: {qualifier} — {decision.reason}"
                f"\n    Proposed: {decision.label}")
    return f"✗ {artist} — {title}: no credible match — {decision.reason}"


@dataclass
class AddResult:
    """What happened to the approved songs in one playlist.

    `refused` means YT Music answered no; `failed` means there was no answer
    (an error), so whether the song went in is unknown. `error` is the first
    error message, if any.
    """

    playlist: str
    added: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None

    def message(self) -> str:
        """The Messages-pane line for this playlist."""
        name = f"'{self.playlist}'"
        if self.failed and not self.added and not self.refused:
            return f"→ Failed to add to {name}: {self.error}"
        attempted = len(self.added) + len(self.refused) + len(self.failed)
        if not attempted:
            return (f"→ {name}: all {len(self.already_present)} song(s) are already "
                    "in the playlist — nothing to add")
        line = (f"→ Added {len(self.added)} song(s) to {name}" if attempted == len(self.added)
                else f"→ Added {len(self.added)} of {attempted} song(s) to {name}")
        if self.already_present:
            line += f" ({len(self.already_present)} already there, skipped)"
        if self.refused:
            line += (f"; {len(self.refused)} refused (playlist not editable, or YT Music "
                     "sees them as duplicates under different ids)")
        if self.failed:
            line += f"; {len(self.failed)} failed ({self.error})"
        return line


def add_video_ids_to_playlists(yt, video_ids, playlists, put) -> list[AddResult]:
    """Add already-approved video ids to each playlist. Returns one AddResult
    per playlist that had anything to add.

    Phase two of adding: by the time this runs, every match has been classified
    and every ambiguous one decided, so nothing here needs to judge anything.
    """
    video_ids = list(dict.fromkeys(video_ids))  # two rows can match the same YT song
    results = []
    for pl in playlists:
        if video_ids:
            result = _add_to_playlist(yt, pl, video_ids)
            results.append(result)
            put(("log", result.message()))
        put(("step", None))
    put(("log", done_summary(results)))
    return results


def done_summary(results) -> str:
    """The closing line: totals over every playlist, counted per song and
    playlist — one song added to two playlists is two additions."""
    if not results:
        return "--- Done. ---"
    parts = [f"{sum(len(r.added) for r in results)} added"]
    for attr, label in (("already_present", "already there"),
                        ("refused", "refused"), ("failed", "failed")):
        count = sum(len(getattr(r, attr)) for r in results)
        if count:
            parts.append(f"{count} {label}")
    return f"--- Done: {', '.join(parts)}, across {len(results)} playlist(s). ---"


def _add_to_playlist(yt, pl, video_ids) -> AddResult:
    """Add the ids to one playlist.

    YT Music rejects a whole batch, adding nothing, if even one song is already
    in the playlist. So a refused batch is retried without the songs the
    playlist already contains — if that removed any; otherwise it would just be
    the same batch again. If it is *still* refused (a duplicate under a
    different id, or a song the playlist won't take), the rest go one at a
    time, so a single refusal can't cost every other new song.
    """
    result = AddResult(pl["title"])
    try:
        if _accepted(yt, pl, video_ids):
            result.added = list(video_ids)
            return result
        tracks = yt.get_playlist(pl["playlistId"], limit=None).get("tracks") or []
        existing = {t.get("videoId") for t in tracks}
        result.already_present = [v for v in video_ids if v in existing]
        to_add = [v for v in video_ids if v not in existing]
        if not to_add:
            return result
        if result.already_present and _accepted(yt, pl, to_add):
            result.added = to_add
            return result
    except Exception as e:
        result.error = str(e)
        result.failed = [v for v in video_ids if v not in result.already_present]
        return result

    for v in to_add:
        try:
            (result.added if _accepted(yt, pl, [v]) else result.refused).append(v)
        except Exception as e:
            result.failed.append(v)
            result.error = result.error or str(e)
    return result


def _accepted(yt, pl, video_ids) -> bool:
    """True if YT Music accepted the batch, False if it refused it. Errors
    propagate: an error is not an answer."""
    result = yt.add_playlist_items(pl["playlistId"], video_ids, duplicates=False)
    status = result.get("status", "") if isinstance(result, dict) else result
    return "SUCCEEDED" in str(status)


def export_playlists_to_csv(yt, playlists, dest, put, liked_only=False):
    """Fetch each playlist's tracks and write a CSV per playlist.

    With liked_only, keep just the tracks whose likeStatus is "LIKE". A like
    belongs to one videoId, so liking another upload of the same song doesn't
    count. A playlist with no liked tracks gets no file.
    """
    for pl in playlists:
        try:
            tracks = yt.get_playlist(pl["playlistId"], limit=None).get("tracks") or []
            if not liked_only:
                path = write_playlist_csv(pl["title"], tracks, dest)
                put(("log", f"→ Saved '{pl['title']}' ({len(tracks)} tracks) to {path.name}"))
            elif liked := [t for t in tracks if t.get("likeStatus") == "LIKE"]:
                path = write_playlist_csv(pl["title"], liked, dest)
                put(("log", f"→ Saved '{pl['title']}' ({len(liked)} liked of "
                            f"{len(tracks)} tracks) to {path.name}"))
            else:
                put(("log", f"→ No liked songs in '{pl['title']}' "
                            f"({len(tracks)} tracks), nothing saved"))
        except Exception as e:
            put(("log", f"→ Failed to export '{pl['title']}': {e}"))
        put(("step", None))
    put(("log", "--- Export done. ---"))
