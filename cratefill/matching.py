"""Song-matching heuristic: decide which search result answers a request.

Deliberately pure — no Tkinter, no ytmusicapi, no I/O — so the same inputs
always give the same answer and the rules are cheap to test. See
tests/test_matching.py. This is the module the improved matching system grows
into (scoring, recording-version detection, a structured MatchDecision).
"""


def normalize(s):
    """Casefold and strip punctuation. Tolerates None: YT Music leaves fields
    like "title" or an artist "name" out (or null) on some results."""
    return "".join(c for c in (s or "").casefold() if c.isalnum() or c.isspace()).strip()


def pick_match(results, artist, title):
    """Pick the best search result. Returns (result, confident) or (None, False).

    Defensive about the shape of `results`: it comes straight from an
    unofficial API, where "artists" can be absent, null, or hold entries
    without a name. A TypeError here used to kill the whole worker thread.
    """
    want_artist, want_title = normalize(artist), normalize(title)
    candidates = [
        r for r in (results or []) if isinstance(r, dict) and r.get("videoId")
    ]
    for r in candidates:
        got_title = normalize(r.get("title"))
        got_artists = [
            normalize(a.get("name"))
            for a in (r.get("artists") or [])
            if isinstance(a, dict)
        ]
        # Both titles must be non-empty: "" is a substring of everything, so a
        # result with no title would otherwise pass as a confident match.
        title_ok = bool(want_title and got_title) and (
            want_title in got_title or got_title in want_title
        )
        artist_ok = not want_artist or any(
            want_artist in a or a in want_artist for a in got_artists if a
        )
        if title_ok and artist_ok:
            return r, True
    if candidates:
        return candidates[0], False
    return None, False
