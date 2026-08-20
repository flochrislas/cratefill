"""Decide which search result — if any — answers a requested song.

Deliberately pure: `rapidfuzz` is the only import, there is no I/O, and the same
inputs always give the same answer. See tests/test_matching.py.

The rules exist to stop Cratefill adding an unrelated song just because YouTube
Music returned it first. Three outcomes:

    high        artist and title both match strongly, no version conflict → add
    ambiguous   credible, but not certain → the user's policy decides
    rejected    fails a minimum, or the recording version conflicts → skip

Two invariants hold everywhere below:

* A great title score never compensates for the wrong artist, and vice versa —
  they are thresholded independently before the weighted score is even used.
* There is no "first result" fallback. If nothing clears the minimums the answer
  is `rejected`, and no policy setting can turn that into an add.
"""

import re
import unicodedata

from rapidfuzz import fuzz

# Tunable starting values. Deliberately strict: a false negative costs the user a
# prompt, a false positive puts a wrong song in their playlist. Loosen only with
# real examples and tests to back it up.
MIN_TITLE = 0.75        # below this → rejected, whatever the artist score says
MIN_ARTIST = 0.80       # below this → rejected, whatever the title score says
HIGH_TITLE = 0.94       # both HIGH_* plus the margin → high confidence
HIGH_ARTIST = 0.92
TITLE_WEIGHT = 0.65     # overall = 0.65 * title + 0.35 * artist
ARTIST_WEIGHT = 0.35
WINNER_MARGIN = 0.08    # a near-tied runner-up makes the winner ambiguous
SEARCH_LIMIT = 10       # candidates to ask YouTube Music for

# Text this short is compared by exact equality only — fuzzy matching on a
# handful of characters is how "Cher" becomes "Cherub".
SHORT_TEXT_LEN = 4

# Markers naming a materially different recording. A mismatch in this set is a
# hard conflict in *either* direction: a requested studio track must not become a
# live version, and a requested live version must not become the studio one.
HARD_VERSION_MARKERS = {
    "live": (r"\blive\b",),
    "remix": (r"\bremix(es|ed)?\b", r"\bmix\b"),
    "acoustic": (r"\bacoustic\b", r"\bunplugged\b"),
    "instrumental": (r"\binstrumental\b",),
    "karaoke": (r"\bkaraoke\b",),
    "cover": (r"\bcover\b", r"\btribute\b"),
    "demo": (r"\bdemo\b",),
    "radio edit": (r"\bradio edit\b",),
    "extended mix": (r"\bextended\b",),
    "sped up": (r"\bsped ?up\b", r"\bnightcore\b"),
    "slowed": (r"\bslowed\b", r"\breverb\b"),
    "clean": (r"\bclean\b",),
    "explicit": (r"\bexplicit\b",),
}

# Markers that describe a re-release or a packaging detail rather than a
# different performance: "Wonderwall" may match "Wonderwall (Remastered)".
SOFT_VERSION_MARKERS = (
    r"\b(19|20)\d{2} remaster(ed)?\b",
    r"\bremaster(ed)?\b",
    r"\banniversary( edition)?\b",
    r"\bdeluxe( edition)?\b",
    r"\bmono\b",
    r"\bstereo\b",
    r"\balbum version\b",
    r"\bsingle version\b",
    r"\boriginal version\b",
    r"\bbonus track\b",
    r"\bofficial (video|audio|music video)\b",
    r"\blyrics? video\b",
    r"\bvisuali[sz]er\b",
)

# "feat." / "featuring" / "ft." / "with", plus the ampersand-free remainder.
FEATURED_RE = re.compile(r"\b(?:feat|featuring|ft|with)\b\.?", re.IGNORECASE)

# Interior !/$ stand in for letters in stylised names (P!nk, Ke$ha). Handled
# before punctuation becomes whitespace, or "P!nk" would split into "p nk".
INTERIOR_LETTER_SUBS = {"!": "i", "$": "s"}

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "ʼ": "'"})

# Letters NFKD leaves alone because they are letters in their own right, not
# accented forms: "Cœur de pirate" must match "Coeur de pirate".
_LIGATURES = {
    "œ": "oe", "æ": "ae", "ø": "o", "ß": "ss", "ł": "l",
    "đ": "d", "ð": "d", "þ": "th", "ı": "i", "ħ": "h",
}


def normalize(text):
    """Casefold and flatten harmless formatting differences.

    Accents are stripped (Beyoncé → beyonce), curly quotes straightened,
    punctuation becomes whitespace (AC/DC → ac dc) and runs of whitespace
    collapse. Tolerates None: YouTube Music omits or nulls fields like "title"
    and an artist's "name" on some results.
    """
    text = (text or "").translate(_QUOTES)
    text = _substitute_interior_letters(text)
    for ligature, plain in _LIGATURES.items():
        if ligature in text or ligature.upper() in text:
            text = text.replace(ligature, plain).replace(ligature.upper(), plain)
    # NFKD splits "é" into "e" + combining accent, which Mn then drops.
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    kept = "".join(c if c.isalnum() else " " for c in stripped.casefold())
    return " ".join(kept.split())


def _substitute_interior_letters(text):
    """Map !/$ to i/s when they sit between two letters, and drop them otherwise."""
    out = []
    for i, char in enumerate(text):
        if char in INTERIOR_LETTER_SUBS:
            before = text[i - 1] if i else ""
            after = text[i + 1] if i + 1 < len(text) else ""
            out.append(INTERIOR_LETTER_SUBS[char] if before.isalpha() and after.isalpha() else " ")
        else:
            out.append(char)
    return "".join(out)


def tokens(text):
    """Normalized whole words. Substring comparison is what let "one" match
    "someone", so everything downstream works on these instead."""
    return normalize(text).split()


def strip_leading_the(word_list):
    """"The Beatles" and "Beatles" name the same band."""
    return word_list[1:] if len(word_list) > 1 and word_list[0] == "the" else word_list


def split_featured(text):
    """Split "Artist feat. Guest" into ("Artist", ["Guest"]).

    Featured artists are parsed out rather than treated as ordinary words, so
    they neither dilute the title score nor mask the principal artist.
    """
    parts = FEATURED_RE.split(text or "")
    main = parts[0].strip(" -–—(),")
    guests = []
    for chunk in parts[1:]:
        for guest in re.split(r"[,&/]| and ", chunk):
            guest = guest.strip(" -–—(),")
            if guest:
                guests.append(guest)
    return main, guests


def version_markers(text):
    """Return (hard, soft) marker names found anywhere in `text`.

    Parenthesised text is *not* stripped first: "(Live at Wembley)" is exactly
    the information that must not be thrown away.
    """
    normalized = normalize(text)
    hard = {
        name
        for name, patterns in HARD_VERSION_MARKERS.items()
        if any(re.search(p, normalized) for p in patterns)
    }
    soft = {p for p in SOFT_VERSION_MARKERS if re.search(p, normalized)}
    return hard, soft


def has_version_conflict(want_title, got_title):
    """True when the two titles describe different recordings.

    Set inequality, so it catches both directions — asking for a studio track and
    being offered a remix, or asking for the live version and being offered the
    studio one.
    """
    return version_markers(want_title)[0] != version_markers(got_title)[0]


def core_title(text):
    """The title with everything that isn't the song's identity removed.

    Drops "feat. X", soft markers like "(Remastered)", and the words naming hard
    markers — hard markers are compared separately by has_version_conflict, so
    leaving them in would double-penalise a legitimately matching live version.
    """
    main, _guests = split_featured(text)
    normalized = normalize(main)
    for pattern in SOFT_VERSION_MARKERS:
        normalized = re.sub(pattern, " ", normalized)
    for patterns in HARD_VERSION_MARKERS.values():
        for pattern in patterns:
            normalized = re.sub(pattern, " ", normalized)
    return " ".join(normalized.split())


def _ratio(want, got):
    """Fuzzy similarity in 0.0–1.0, tolerant of word order."""
    return max(fuzz.token_set_ratio(want, got), fuzz.WRatio(want, got)) / 100.0


def score_text(want, got):
    """Score two pieces of text 0.0–1.0 on whole tokens.

    Exact token equality scores 1.0. Otherwise the tokens must genuinely overlap
    before any fuzzy score is trusted — that gate is what stops "one" matching
    "someone" and "cher" matching "cherub", where character similarity is high
    but no whole word is shared. Overlap is measured against the *longer* side,
    so "hello" cannot pass as "hello world goodbye" either.
    """
    want_tokens, got_tokens = tokens(want), tokens(got)
    if not want_tokens or not got_tokens:
        return 0.0
    want_set, got_set = set(want_tokens), set(got_tokens)
    if want_tokens == got_tokens or want_set == got_set:  # same words, any order
        return 1.0

    shared = want_set & got_set
    if not shared:
        return 0.0
    # Very short values get no fuzzy credit at all: three or four characters are
    # too few for a similarity ratio to mean anything.
    if min(len(" ".join(want_tokens)), len(" ".join(got_tokens))) <= SHORT_TEXT_LEN:
        return 0.0
    coverage = len(shared) / max(len(want_set), len(got_set))
    return min(_ratio(" ".join(want_tokens), " ".join(got_tokens)), coverage)


def score_artist(want_artist, result_artists):
    """Best score for the requested artist against a result's artist list.

    The requested principal artist must be represented; extra artists on the
    result (featured guests, collaborators) don't penalise it. Requested guests
    can match any of the returned artists. Malformed entries are ignored.
    """
    names = [
        a.get("name")
        for a in (result_artists or [])
        if isinstance(a, dict) and a.get("name")
    ]
    if not names:
        return 0.0

    principal, guests = split_featured(want_artist)
    wanted = [w for w in [principal, *guests] if tokens(w)]
    if not wanted:
        return 0.0

    def best(one):
        one_tokens = strip_leading_the(tokens(one))
        scores = [0.0]
        for name in names:
            name_tokens = strip_leading_the(tokens(name))
            scores.append(score_text(" ".join(one_tokens), " ".join(name_tokens)))
        # The result may bundle every artist into one string ("Air, Phoenix").
        scores.append(score_text(" ".join(one_tokens), " ".join(names)))
        return max(scores)

    # The principal artist carries the requirement; guests can only help.
    return max(best(w) for w in wanted) if len(wanted) > 1 else best(principal)


def artists_label(result):
    """Human-readable artist string for a search result, for logs and dialogs."""
    return ", ".join(
        a.get("name") or ""
        for a in (result.get("artists") or [])
        if isinstance(a, dict)
    )


def validate_request(artist, title):
    """Reasons this imported row can't be matched automatically (empty if fine).

    Checked before searching, so a hopeless row doesn't cost a network call.
    """
    reasons = []
    if not tokens(title):
        reasons.append("no title in the imported row")
    if not tokens(artist):
        reasons.append("no artist in the imported row")
    return reasons


class MatchDecision:
    """What the evidence says about a requested song. Carries no policy."""

    __slots__ = (
        "status", "candidate", "title_score", "artist_score",
        "overall_score", "runner_up_score", "reasons", "alternatives",
    )

    def __init__(self, status, candidate=None, title_score=0.0, artist_score=0.0,
                 overall_score=0.0, runner_up_score=None, reasons=None, alternatives=None):
        self.status = status                  # "high" | "ambiguous" | "rejected"
        self.candidate = candidate            # the winning search result, or None
        self.title_score = title_score
        self.artist_score = artist_score
        self.overall_score = overall_score
        self.runner_up_score = runner_up_score
        self.reasons = reasons or []
        self.alternatives = alternatives or []

    def __repr__(self):
        return (
            f"MatchDecision({self.status!r}, title={self.title_score:.2f}, "
            f"artist={self.artist_score:.2f}, overall={self.overall_score:.2f}, "
            f"runner_up={self.runner_up_score}, reasons={self.reasons!r})"
        )

    @property
    def reason(self):
        """The reasons as one sentence, for logs and the review dialog."""
        return "; ".join(self.reasons)

    @property
    def video_id(self):
        return (self.candidate or {}).get("videoId")

    @property
    def label(self):
        """"Artist — Title" of the proposed match, or "" when there is none."""
        if not self.candidate:
            return ""
        return f"{artists_label(self.candidate)} — {self.candidate.get('title') or ''}"


def choose_match(artist, title, results):
    """Evaluate search results for one requested song. Returns a MatchDecision.

    Only evaluates evidence — it does not know or care whether ambiguous matches
    end up added, skipped or shown to the user. See policy.action_for_match.

    Defensive about the shape of `results`: it comes straight from an unofficial
    API where entries may not be dicts and "artists" may be absent, null, or
    hold nameless entries.
    """
    blocking = validate_request(artist, title)
    if blocking:
        return MatchDecision("rejected", reasons=blocking)

    scored = []
    for result in results or []:
        if not isinstance(result, dict) or not result.get("videoId"):
            continue  # no videoId means nothing can be added
        title_score = score_text(core_title(title), core_title(result.get("title")))
        artist_score = score_artist(artist, result.get("artists"))
        conflict = has_version_conflict(title, result.get("title"))
        overall = TITLE_WEIGHT * title_score + ARTIST_WEIGHT * artist_score
        scored.append((overall, title_score, artist_score, conflict, result))

    if not scored:
        return MatchDecision("rejected", reasons=["no usable search results"])

    # Credible candidates only: both minimums met and no conflicting version.
    # Anything else can still be reported, but must never be added.
    credible = [s for s in scored
                if s[1] >= MIN_TITLE and s[2] >= MIN_ARTIST and not s[3]]
    scored.sort(key=lambda s: s[0], reverse=True)

    if not credible:
        overall, title_score, artist_score, conflict, result = scored[0]
        return MatchDecision(
            "rejected",
            candidate=None,          # deliberately not offered: nothing here is addable
            title_score=title_score,
            artist_score=artist_score,
            overall_score=overall,
            reasons=_rejection_reasons(title_score, artist_score, conflict, title, result),
            alternatives=[s[4] for s in scored[:3]],
        )

    credible.sort(key=lambda s: s[0], reverse=True)
    overall, title_score, artist_score, _conflict, result = credible[0]
    runner_up = credible[1][0] if len(credible) > 1 else None

    reasons = []
    if title_score < HIGH_TITLE:
        reasons.append(f"title similarity {title_score:.2f} below {HIGH_TITLE:.2f}")
    if artist_score < HIGH_ARTIST:
        reasons.append(f"artist similarity {artist_score:.2f} below {HIGH_ARTIST:.2f}")
    if runner_up is not None and overall - runner_up < WINNER_MARGIN:
        reasons.append(
            f"another candidate scores almost the same ({runner_up:.2f} vs {overall:.2f})"
        )

    return MatchDecision(
        "ambiguous" if reasons else "high",
        candidate=result,
        title_score=title_score,
        artist_score=artist_score,
        overall_score=overall,
        runner_up_score=runner_up,
        reasons=reasons,
        alternatives=[s[4] for s in credible[1:4]],
    )


def _rejection_reasons(title_score, artist_score, conflict, want_title, result):
    reasons = []
    if title_score < MIN_TITLE:
        reasons.append(f"title similarity {title_score:.2f} below {MIN_TITLE:.2f}")
    if artist_score < MIN_ARTIST:
        reasons.append(f"artist similarity {artist_score:.2f} below {MIN_ARTIST:.2f}")
    if conflict and not reasons:
        want = version_markers(want_title)[0]
        got = version_markers(result.get("title"))[0]
        differing = sorted(want ^ got) or ["version"]
        reasons.append(f"different recording ({', '.join(differing)})")
    return reasons or ["no candidate met the minimum scores"]
