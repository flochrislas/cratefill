"""Decide which search result — if any — answers a requested song.

Deliberately pure: `rapidfuzz` is the only import, there is no I/O, and the same
inputs always give the same answer. See tests/test_matching.py.

Four outcomes:

    high        near-identical artist and title, the recording asked for, and a
                clear win over the runner-up → added without asking
    ambiguous   recognisably the same song, but something is off → the user's
                policy decides (ask / skip / add)
    weak        plausibly the same song, on thin evidence → always asks, whatever
                the policy says
    rejected    nothing here is the same song → skipped

The balance being struck: **coming back empty-handed is the worst outcome.** If
YouTube Music has a remix, a live take, or another artist's cover of the song
that was asked for, that is worth offering — a playlist entry the user can
review beats a silent miss. So a version difference or a wrong artist no longer
excludes a candidate; it costs it points (VERSION_PENALTY) and blocks the `high`
tier, which is what keeps the offer honest rather than silent. `weak` exists
because "the user can review it" is only true when the user is actually asked, so
the thinnest evidence is never handed to a saved "Always add".

What still gets refused outright is a *different song*. `rejected` means no
result shared a **content word** with the requested title: `has_content_overlap`
discounts STOP_WORDS, so "The End" and "The Beginning" don't qualify on "the".
That is the rule that keeps `One` from becoming `Someone`, `Cher` from becoming
`Cherub`, and `Lisztomania` from becoming some other Phoenix track. Scripts
without word boundaries (CJK, Thai) have no whole words to share, so
`_score_spaceless` supplies a script-gated character-similarity fallback there.

`high` is deliberately hard to earn — everything doubtful is offered, not
assumed. Nothing here knows what an ambiguous match should *lead to*; see
policy.action_for_match.
"""

import re
import unicodedata
from collections import Counter

from rapidfuzz import fuzz

# Tunable values. The balance they encode: coming back empty-handed is the worst
# outcome, so anything recognisably the same song gets offered; being *confident*
# is what stays hard to earn.
TITLE_FLOOR = 0.0       # at or below this the titles are unrelated → rejected
HIGH_TITLE = 0.90       # both HIGH_* plus the margin and an exact version → high
HIGH_ARTIST = 0.88
TITLE_WEIGHT = 0.65     # base = 0.65 * title + 0.35 * artist
ARTIST_WEIGHT = 0.35
WINNER_MARGIN = 0.05    # a near-tied runner-up makes the winner ambiguous
SEARCH_LIMIT = 10       # candidates to ask YouTube Music for

# Below either of these the evidence is too thin to hand to an "Always add"
# policy: the match is still offered, but it always asks. See _classify().
WEAK_TITLE = 0.80
WEAK_ARTIST = 0.50

# How much a recording-version difference costs a candidate when ranking. Folded
# into the score rather than used to exclude, so a version difference can be
# outweighed — the real artist's live take should beat a tribute band's studio
# cut. Getting the plain recording you didn't ask for ("missing") is a milder
# disappointment than getting a remix you never wanted ("extra").
VERSION_PENALTY = {"same": 0.0, "missing": 0.10, "extra": 0.20}

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

# Featured-artist separators. "with" only counts in *artist* names ("Ella
# Fitzgerald with Louis Armstrong") — in a title it is an ordinary word, and
# treating it as a separator turned "With or Without You" into an empty string.
TITLE_FEATURED_RE = re.compile(r"\b(?:feat|featuring|ft)\b\.?", re.IGNORECASE)
ARTIST_FEATURED_RE = re.compile(r"\b(?:feat|featuring|ft|with)\b\.?", re.IGNORECASE)

# Words too common to establish that two titles are about the same song. Without
# this, "The End" and "The Beginning" share a word and count as related.
STOP_WORDS = frozenset("""
a an and as at be by de del di do e el en et for from i in is it its la le les
me my no not of on or the to un une up us we with you your
""".split())

# Guest artists can only ever nudge a score; the principal artist has to be
# there. Small enough that guests alone stay far below HIGH_ARTIST.
GUEST_BONUS = 0.05

# Character-similarity floor for scripts that don't put spaces between words.
SPACELESS_MIN = 0.80

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


def split_featured(text, allow_with=False):
    """Split "Artist feat. Guest" into ("Artist", ["Guest"]).

    Featured artists are parsed out rather than treated as ordinary words, so
    they neither dilute the title score nor mask the principal artist.

    `allow_with` treats "with" as a separator too. Only pass it for artist
    names: in a title "with" is an ordinary word, and splitting on it reduced
    "With or Without You" to nothing at all.
    """
    pattern = ARTIST_FEATURED_RE if allow_with else TITLE_FEATURED_RE
    parts = pattern.split(text or "")
    main = parts[0].strip(" -–—(),")
    guests = []
    for chunk in parts[1:]:
        for guest in re.split(r"[,&/]| and ", chunk):
            guest = guest.strip(" -–—(),")
            if guest:
                guests.append(guest)
    return main, guests


# A trailing " - Live at Wembley" is metadata; YouTube Music uses both that and
# the bracketed form.
BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
TRAILING_METADATA_RE = re.compile(r"\s[-–—]\s.*$")


def metadata_segments(text):
    """The parts of a title that describe the *recording* rather than the song.

    Only bracketed groups and a trailing dash-separated segment count. Scanning
    the whole title instead meant a song actually called "Clean", "Stereo" or
    "Live and Let Die" was read as version metadata and erased.
    """
    text = text or ""
    segments = BRACKETED_RE.findall(text)
    trailing = TRAILING_METADATA_RE.search(BRACKETED_RE.sub(" ", text))
    if trailing:
        segments.append(trailing.group(0))
    return segments


def version_markers(text):
    """Return (hard, soft) marker names, read from metadata positions only."""
    scanned = normalize(" ".join(metadata_segments(text)))
    hard = {
        name
        for name, patterns in HARD_VERSION_MARKERS.items()
        if any(re.search(p, scanned) for p in patterns)
    }
    soft = {p for p in SOFT_VERSION_MARKERS if re.search(p, scanned)}
    return hard, soft


def version_relation(want_title, got_title):
    """How the candidate's recording relates to the requested one.

    Deliberately asymmetric, because the two directions are not equally bad:

    "extra"   the candidate carries a marker that wasn't asked for — a remix, a
              live take, a karaoke version when the plain song was requested.
              Offered, but never silently: it costs the most (VERSION_PENALTY)
              and can never be `high`, so the user always sees it first.
    "missing" the candidate is the *less* specific recording: the live (or
              acoustic, or remix) version was requested and only the standard
              one came back. Also offered, and penalised less — getting the
              album version is a milder disappointment than getting a remix
              nobody asked for.
    "same"    the markers agree.

    Neither difference *excludes* a candidate. Filtering on version was tried and
    was wrong: it let a tribute band's exact studio cut outrank the real artist's
    live take.
    """
    if normalize(want_title) == normalize(got_title):
        return "same"  # literally the same string; nothing to compare
    want, got = version_markers(want_title)[0], version_markers(got_title)[0]
    if got - want:
        return "extra"
    if want - got:
        return "missing"
    return "same"


def has_version_conflict(want_title, got_title):
    """True when the two titles don't describe the same recording."""
    return version_relation(want_title, got_title) != "same"


def core_title(text):
    """The title with everything that isn't the song's identity removed.

    Only metadata *positions* are stripped — bracketed groups naming a version,
    and a trailing dash-separated segment — and a bracketed group goes whole
    rather than word by word: "(Live at Wembley)" is one piece of metadata, and
    keeping "at wembley" would make the live take look like a different song.
    Words outside those positions are part of the title, however marker-ish they
    look; "Clean", "Stereo" and "Live and Let Die" are songs.

    Never returns "" for a non-empty title: if stripping would empty it, the
    metadata *was* the title, so the whole normalized title is kept. That
    backstop is what stops a future addition to the marker lists from making a
    song unmatchable.

    Version markers are compared separately by version_relation(), so removing
    them here is what lets a legitimately matching live version score 1.00.
    """
    remainder = text or ""
    for segment in metadata_segments(remainder):
        if any(version_markers(segment)):
            remainder = remainder.replace(segment, " ")
    main, _guests = split_featured(remainder)
    core = " ".join(normalize(main).split())
    return core or normalize(text)


def _ratio(want, got):
    """Fuzzy similarity in 0.0–1.0, tolerant of word order.

    Deliberately *not* token_set_ratio or WRatio: both deduplicate tokens, which
    scored "Run Run Run" and "Run" as identical. token_sort_ratio keeps
    multiplicity while still ignoring order.
    """
    return max(fuzz.token_sort_ratio(want, got), fuzz.ratio(want, got)) / 100.0


def _is_spaceless_script(text):
    """True for scripts that don't separate words with spaces (CJK, Thai…).

    The whole-token gate below can't work on those: the entire title is one
    token, so any spelling variation shares nothing.
    """
    return any(
        "぀" <= c <= "ヿ"      # hiragana, katakana
        or "㐀" <= c <= "鿿"   # CJK ideographs
        or "豈" <= c <= "﫿"   # CJK compatibility ideographs
        or "가" <= c <= "힯"   # hangul syllables
        or "฀" <= c <= "๿"   # thai
        for c in text or ""
    )


def score_text(want, got):
    """Score two pieces of text 0.0–1.0 on whole tokens.

    Equal token *multisets* score 1.0 — order is irrelevant, repetition is not.
    Otherwise the tokens must genuinely overlap before any fuzzy score is
    trusted: that gate is what stops "one" matching "someone" and "cher"
    matching "cherub", where character similarity is high but no whole word is
    shared. Overlap is measured against the *longer* side, so "hello" cannot
    pass as "hello world goodbye" either.
    """
    want_tokens, got_tokens = tokens(want), tokens(got)
    if not want_tokens or not got_tokens:
        return 0.0
    want_counts, got_counts = Counter(want_tokens), Counter(got_tokens)
    if want_counts == got_counts:  # same words the same number of times
        return 1.0

    want_text, got_text = " ".join(want_tokens), " ".join(got_tokens)
    shared = sum((want_counts & got_counts).values())
    if not shared:
        return _score_spaceless(want_text, got_text)
    # Very short values get no fuzzy credit at all: three or four characters are
    # too few for a similarity ratio to mean anything. Only when *both* sides are
    # a single word, though — "Run Run Run" against "Run" shares a whole word and
    # deserves a partial score, whereas "cher"/"cherub" share nothing.
    if (len(want_tokens) == 1 and len(got_tokens) == 1
            and min(len(want_text), len(got_text)) <= SHORT_TEXT_LEN):
        return 0.0
    coverage = shared / max(sum(want_counts.values()), sum(got_counts.values()))
    return min(_ratio(want_text, got_text), coverage)


def _score_spaceless(want_text, got_text):
    """Character-similarity fallback for scripts without word boundaries.

    Gated on script and on both sides being a single token, so it cannot revive
    the substring behaviour this module exists to prevent — "one"/"someone" are
    Latin and multi-character-similar, and stay at 0.0.
    """
    if " " in want_text or " " in got_text:
        return 0.0
    if not (_is_spaceless_script(want_text) or _is_spaceless_script(got_text)):
        return 0.0
    if min(len(want_text), len(got_text)) < 3:
        return 0.0
    ratio = fuzz.ratio(want_text, got_text) / 100.0
    return ratio if ratio >= SPACELESS_MIN else 0.0


def score_title(want, got):
    """Score two titles 0.0–1.0.

    Identical normalized titles score 1.0 before any metadata stripping happens
    — otherwise a song whose title *is* metadata ("Clean") could be reduced to
    nothing and fail to match itself.
    """
    if normalize(want) == normalize(got):
        return 1.0
    return score_text(core_title(want), core_title(got))


def score_artist(want_artist, result_artists):
    """Score the requested artist against a result's artist list.

    Returns `(principal, combined)`, both 0.0–1.0, and the two are **not**
    interchangeable:

    * `principal` is how well the requested principal artist is represented. It
      alone decides whether a match may be `high`.
    * `combined` adds GUEST_BONUS per matched guest and is for ranking only.

    Keeping them apart is the point. Collapsing them let a guest bonus lift a
    near-miss principal over HIGH_ARTIST — "Nick Cave and the Bad Seeds" against
    "Nick Cave & The Bad Seeds" scores 0.833, and one matching guest made 0.883,
    clearing the 0.88 gate the principal had failed. Guests help a candidate
    *win*; they never make it certain.

    Extra artists on the result (guests, collaborators) never penalise it, and
    malformed entries are ignored.
    """
    names = [
        a.get("name")
        for a in (result_artists or [])
        if isinstance(a, dict) and a.get("name")
    ]
    if not names:
        return 0.0, 0.0

    principal, guests = split_featured(want_artist, allow_with=True)
    if not tokens(principal):
        return 0.0, 0.0

    def best(one):
        one_text = " ".join(strip_leading_the(tokens(one)))
        scores = [score_text(one_text, " ".join(strip_leading_the(tokens(name))))
                  for name in names]
        # The result may bundle every artist into one string ("Air, Phoenix").
        scores.append(score_text(one_text, " ".join(names)))
        return max(scores, default=0.0)

    principal_score = best(principal)
    found_guests = sum(1 for guest in guests if tokens(guest) and best(guest) >= HIGH_ARTIST)
    return principal_score, min(1.0, principal_score + GUEST_BONUS * found_guests)


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


def has_content_overlap(want_title, got_title):
    """True when the titles share a word that actually says something.

    "The End" and "The Beginning" share "the", which is no evidence at all —
    and under an "Always add" policy that word alone could authorise a
    completely different song. Titles built entirely from stop words ("You and
    Me") fall back to any shared token, or they could never match anything.
    """
    want, got = Counter(tokens(core_title(want_title))), Counter(tokens(core_title(got_title)))
    shared = set((want & got).elements())
    if shared - STOP_WORDS:
        return True
    if shared:  # only stop words in common — evidence only if that's all there is
        return not (set(want) - STOP_WORDS)
    # No shared token at all is normally decisive, but scripts without word
    # boundaries have only one token to begin with; there score_text falls back
    # to character similarity, and that score is the evidence.
    return _score_spaceless(" ".join(want.elements()), " ".join(got.elements())) > 0.0


class Candidate:
    """One scored search result, with the reasons it isn't a perfect answer.

    Scoring lives here rather than in the UI so the review dialog can list
    alternatives without recomputing anything.
    """

    __slots__ = ("result", "title_score", "artist_score", "principal_score",
                 "overall_score", "relation", "reasons")

    def __init__(self, result, title_score, artist_score, overall_score, relation,
                 reasons=None, principal_score=None):
        self.result = result
        self.title_score = title_score
        self.artist_score = artist_score        # with the guest bonus: for ranking
        # Without it: the only artist number allowed to decide confidence.
        self.principal_score = artist_score if principal_score is None else principal_score
        self.overall_score = overall_score
        self.relation = relation
        self.reasons = reasons or []

    @property
    def video_id(self):
        return (self.result or {}).get("videoId")

    @property
    def label(self):
        """"Artist — Title", for logs and the review dialog."""
        return f"{artists_label(self.result)} — {self.result.get('title') or ''}"

    @property
    def reason(self):
        return "; ".join(self.reasons)

    def __repr__(self):
        return f"Candidate({self.label!r}, overall={self.overall_score:.2f})"


class MatchDecision:
    """What the evidence says about a requested song. Carries no policy."""

    __slots__ = (
        "status", "candidate", "title_score", "artist_score",
        "overall_score", "runner_up_score", "reasons", "alternatives",
    )

    def __init__(self, status, candidate=None, title_score=0.0, artist_score=0.0,
                 overall_score=0.0, runner_up_score=None, reasons=None, alternatives=None):
        self.status = status         # "high" | "ambiguous" | "weak" | "rejected"
        self.candidate = candidate  # the winning Candidate, or None
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
        return self.candidate.video_id if self.candidate else None

    @property
    def label(self):
        """"Artist — Title" of the proposed match, or "" when there is none."""
        return self.candidate.label if self.candidate else ""

    @property
    def choices(self):
        """The proposal followed by its alternatives, for the review dialog."""
        return ([self.candidate] if self.candidate else []) + list(self.alternatives)


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
        title_score = score_title(title, result.get("title"))
        principal_score, artist_score = score_artist(artist, result.get("artists"))
        relation = version_relation(title, result.get("title"))
        base = TITLE_WEIGHT * title_score + ARTIST_WEIGHT * artist_score
        candidate = Candidate(
            result, title_score, artist_score,
            base * (1.0 - VERSION_PENALTY[relation]), relation,
            principal_score=principal_score,
        )
        candidate.reasons = _shortfalls(candidate, title)
        scored.append(candidate)

    if not scored:
        return MatchDecision("rejected", reasons=["no usable search results"])

    scored.sort(key=lambda c: c.overall_score, reverse=True)

    # The only reason to refuse outright: nothing here is recognisably the same
    # song. A shared *content* word is required — matching on "the" alone is no
    # evidence at all, and under an "Always add" policy it would authorise a
    # different song unreviewed. This rejects "One" → "Someone" and
    # "Lisztomania" → "1901" while still offering a remix, a live take or
    # another artist's cover of the right song.
    related = [
        c for c in scored
        if c.title_score > TITLE_FLOOR and has_content_overlap(title, c.result.get("title"))
    ]
    if not related:
        return MatchDecision(
            "rejected",
            candidate=None,  # deliberately not offered: it's a different song
            title_score=scored[0].title_score,
            artist_score=scored[0].artist_score,
            reasons=["no result with a related title"],
            alternatives=scored[:3],
        )

    winner, *rest = related
    runner_up = rest[0].overall_score if rest else None
    reasons = list(winner.reasons)
    if runner_up is not None and winner.overall_score - runner_up < WINNER_MARGIN:
        reasons.append(
            f"another candidate scores almost the same "
            f"({runner_up:.2f} vs {winner.overall_score:.2f})"
        )

    return MatchDecision(
        _classify(winner, reasons),
        candidate=winner,
        title_score=winner.title_score,
        artist_score=winner.artist_score,
        overall_score=winner.overall_score,
        runner_up_score=runner_up,
        reasons=reasons,
        alternatives=rest[:3],
    )


def _shortfalls(candidate, want_title):
    """Every way this candidate falls short of being the obvious answer."""
    reasons = []
    if candidate.title_score < HIGH_TITLE:
        reasons.append(
            f"title similarity {candidate.title_score:.2f} below {HIGH_TITLE:.2f}"
        )
    # The *principal* score is what's tested, never the guest-boosted one: a
    # guest may help this candidate win, but it can't make it certain.
    if candidate.principal_score < HIGH_ARTIST:
        reasons.append(
            f"artist similarity {candidate.principal_score:.2f} below {HIGH_ARTIST:.2f}"
            f" (found {artists_label(candidate.result) or 'no artist'})"
        )
    if candidate.relation != "same":
        reasons.append(_version_reason(candidate.relation, want_title, candidate.result))
    return reasons


def _classify(winner, reasons):
    """high / ambiguous / weak for a candidate that cleared the relatedness bar.

    `weak` exists because "offer it and let the user glance at it" only holds
    when the user is actually asked. A loose title overlap or a wholly different
    performer is too thin to hand to an "Always add" policy, so it is pinned to
    the ask path — see policy.action_for_match.
    """
    if not reasons:
        return "high"
    if winner.title_score < WEAK_TITLE or winner.principal_score < WEAK_ARTIST:
        return "weak"
    return "ambiguous"


def _version_reason(relation, want_title, result):
    """Explain a recording-version difference in the user's terms."""
    want = version_markers(want_title)[0]
    got = version_markers(result.get("title"))[0]
    if relation == "missing":
        wanted = ", ".join(sorted(want - got))
        return f"no {wanted} version found — this is the standard recording"
    extra = ", ".join(sorted(got - want)) or "different"
    return f"this is the {extra} version, not the one asked for"


