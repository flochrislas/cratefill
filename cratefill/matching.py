"""Decide which search result — if any — answers a requested song.

Deliberately pure: `rapidfuzz` is the only import, there is no I/O, and the same
inputs always give the same answer. See tests/test_matching.py.

Four outcomes:

    high        exactly one version of the song by the requested artist → added
                without asking
    ambiguous   several versions to choose from, or a recognisably related song
                with something off → the user's policy decides (ask/skip/add)
    weak        plausibly the same song, on thin evidence → always asks, whatever
                the policy says
    rejected    nothing here is the same song → skipped

The balance being struck: **coming back empty-handed is the worst outcome.** If
YouTube Music has a remix, a live take, or another artist's cover of the song
that was asked for, that is worth offering — a playlist entry the user can
review beats a silent miss. So a version difference or a wrong artist doesn't
exclude a candidate; it costs it points (VERSION_PENALTY), and a wrong artist
keeps it out of `high`, which is what keeps the offer honest. `weak` exists
because "the user can review it" is only true when the user is actually asked, so
the thinnest evidence is never handed to a saved "Always add".

What still gets refused outright is a *different song*. `rejected` means no
result shared a **content word** with the requested title: `has_content_overlap`
discounts STOP_WORDS, so "The End" and "The Beginning" don't qualify on "the".
That is the rule that keeps `One` from becoming `Someone`, `Cher` from becoming
`Cherub`, and `Lisztomania` from becoming some other Phoenix track. Scripts
without word boundaries (CJK, Thai) have no whole words to share, so
`_score_spaceless` supplies a script-gated character-similarity fallback there.

A *version* of the requested song is a result whose title and principal artist
both reach the HIGH_* thresholds once spacing and anything in brackets or after
a trailing " - " are discounted. One version is the answer; several are a choice
for the user. Nothing here knows what an ambiguous match should *lead to*; see
policy.action_for_match.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import KW_ONLY, dataclass, field

from rapidfuzz import fuzz

# Tunable values. The balance they encode: coming back empty-handed is the worst
# outcome, so anything recognisably the same song gets offered; being *confident*
# is what stays hard to earn.
HIGH_TITLE = 0.90       # a result at or above both HIGH_* is a version of the song
HIGH_ARTIST = 0.88
TITLE_WEIGHT = 0.65     # base = 0.65 * title + 0.35 * artist
ARTIST_WEIGHT = 0.35
SEARCH_LIMIT = 10       # candidates to ask YouTube Music for

# Two results with the same title are the same recording (album and single
# releases) unless their explicit flags differ or their lengths differ by more
# than this — then they are a choice, like any two versions.
SAME_RECORDING_SECONDS = 10

# Below either of these the evidence is too thin to hand to an "Always add"
# policy: the match is still offered, but it always asks.
WEAK_TITLE = 0.80
WEAK_ARTIST = 0.50

# How much a recording-version difference costs a candidate when ranking. Folded
# into the score rather than used to exclude, so a version difference can be
# outweighed — the real artist's live take should beat a tribute band's studio
# cut. Getting the plain recording you didn't ask for ("missing") is a milder
# disappointment than getting a remix you never wanted ("extra").
VERSION_PENALTY = {"same": 0.0, "missing": 0.10, "extra": 0.20}

# Markers naming a materially different recording, read from metadata positions
# only. They rank candidates (VERSION_PENALTY) and explain the difference to the
# user; they never exclude one.
VERSION_MARKERS = {
    "live": (r"\blive\b",),
    # "Original Mix" is the standard release in electronic music, not a remix.
    "remix": (r"\bremix(es|ed)?\b", r"(?<!original )\bmix\b"),
    "acoustic": (r"\bacoustic\b", r"\bunplugged\b"),
    "instrumental": (r"\binstrumental\b",),
    "karaoke": (r"\bkaraoke\b",),
    "cover": (r"\bcover\b", r"\btribute\b"),
    "demo": (r"\bdemo\b",),
    "extended mix": (r"\bextended\b",),
    "sped up": (r"\bsped ?up\b", r"\bnightcore\b"),
    "slowed": (r"\bslowed\b", r"\breverb\b"),
    "clean": (r"\bclean\b",),
    "explicit": (r"\bexplicit\b",),
    "reprise": (r"\breprise\b",),
    "medley": (r"\bmedley\b",),
}

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


def normalize(text: str | None) -> str:
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


def tokens(text: str | None) -> list[str]:
    """Normalized whole words. Substring comparison is what let "one" match
    "someone", so everything downstream works on these instead."""
    return normalize(text).split()


def strip_leading_the(word_list: list[str]) -> list[str]:
    """"The Beatles" and "Beatles" name the same band."""
    return word_list[1:] if len(word_list) > 1 and word_list[0] == "the" else word_list


def split_featured(text: str | None, allow_with: bool = False) -> tuple[str, list[str]]:
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


# Metadata positions: any bracket pair (including the full-width forms common in
# CJK titles) and, in YouTube Music's text only, a trailing " - …" segment.
# Openers are excluded inside a match so nested groups peel innermost first.
BRACKETED_RE = re.compile(
    r"[(\[{<（【［「『][^()\[\]{}<>（）【】［］「」『』]*[)\]}>）】］」』]")
TRAILING_METADATA_RE = re.compile(r"\s[-–—]\s.*$")


def split_metadata(text: str | None, dash: bool = True) -> tuple[str, list[str]]:
    """Split text into (remainder, metadata segments).

    Only bracketed groups and a trailing dash-separated segment count as
    metadata. Scanning the whole title instead meant a song actually called
    "Clean", "Stereo" or "Live and Let Die" was read as version metadata and
    erased.

    dash=False keeps the trailing segment. In YouTube Music's text " - …" is
    always metadata ("Song - Remastered 2011"); in the user's it can be either
    — a Spotify export's "Bohemian Rhapsody - Remastered 2011", or a folder
    import's whole filename, "Phoenix - Lisztomania", where stripping left only
    "Phoenix". So user titles are read both ways; see user_title_cores().
    """
    text, segments = text or "", []
    while found := BRACKETED_RE.findall(text):
        segments += found
        text = BRACKETED_RE.sub(" ", text)
    trailing = dash and TRAILING_METADATA_RE.search(text)
    if trailing:
        segments.append(trailing.group(0))
        text = text[:trailing.start()]
    return text, segments


def strip_metadata(text: str | None, dash: bool = True) -> str:
    """The text without its metadata — or whole, if the metadata *was* the text.

    That backstop is what keeps "(Intro)" matchable: stripping must never turn a
    non-empty title or name into nothing.
    """
    remainder = split_metadata(text, dash)[0]
    return remainder if tokens(remainder) else (text or "")


def version_markers(text: str | None) -> set[str]:
    """Names of the VERSION_MARKERS found in the text's metadata positions."""
    scanned = normalize(" ".join(split_metadata(text)[1]))
    return {
        name
        for name, patterns in VERSION_MARKERS.items()
        if any(re.search(p, scanned) for p in patterns)
    }


def version_relation(want_title: str, got_title: str | None) -> str:
    """How the candidate's recording relates to the requested one.

    Deliberately asymmetric, because the two directions are not equally bad:

    "extra"   the candidate carries a marker that wasn't asked for — a remix, a
              live take, a karaoke version when the plain song was requested.
              Costs the most (VERSION_PENALTY).
    "missing" the candidate is the *less* specific recording: the live version
              was requested and only the standard one came back. Penalised
              less — getting the album version is a milder disappointment than
              getting a remix nobody asked for.
    "same"    the markers agree.

    Neither difference *excludes* a candidate. Filtering on version was tried and
    was wrong: it let a tribute band's exact studio cut outrank the real artist's
    live take.
    """
    return _relation(_Request.of("", want_title), _Result.of({"title": got_title}))


def core_title(text: str | None, dash: bool = True) -> str:
    """The title as compared: metadata and featured artists removed, normalized.

    Words outside metadata positions stay, however marker-ish they look —
    "Clean", "Stereo" and "Live and Let Die" are songs. Never "" for a
    non-empty title. `dash` as in split_metadata.
    """
    main, _guests = split_featured(strip_metadata(text, dash))
    return normalize(main) or normalize(text)


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


def score_text(want: str | None, got: str | None) -> float:
    """Score two pieces of text 0.0–1.0 on whole tokens.

    Equal token *multisets* score 1.0 — order is irrelevant, repetition is not.
    Otherwise the tokens must genuinely overlap before any fuzzy score is
    trusted: that gate is what stops "one" matching "someone" and "cher"
    matching "cherub", where character similarity is high but no whole word is
    shared. Overlap is measured against the *longer* side, so "hello" cannot
    pass as "hello world goodbye" either.
    """
    want_tokens, got_tokens = align_spacing(tokens(want), tokens(got))
    if not want_tokens or not got_tokens:
        return 0.0
    want_counts, got_counts = Counter(want_tokens), Counter(got_tokens)
    if want_counts == got_counts:  # same words the same number of times
        return 1.0

    want_text, got_text = " ".join(want_tokens), " ".join(got_tokens)
    shared = sum((want_counts & got_counts).values())
    if not shared:
        return _score_spaceless(want_text, got_text)
    coverage = shared / max(sum(want_counts.values()), sum(got_counts.values()))
    return min(_ratio(want_text, got_text), coverage)


def align_spacing(want_tokens: list[str], got_tokens: list[str]) -> tuple[list[str], list[str]]:
    """Both token lists, with words split on one side only re-joined.

    "ArtistName" is "Artist Name", "Lovesong Radio Edit" is "Love Song Radio
    Edit": a missing or extra space is spelling, not a different word. Adjacent
    words are joined only into a word the *other* side contains, so "one"
    still can't match "someone".
    """
    want_tokens = _join_split_words(want_tokens, set(got_tokens))
    return want_tokens, _join_split_words(got_tokens, set(want_tokens))


def _join_split_words(words, vocabulary):
    """Greedily join runs of adjacent words that spell a word in `vocabulary`."""
    out, i = [], 0
    while i < len(words):
        for end in range(len(words), i + 1, -1):    # longest run first
            if "".join(words[i:end]) in vocabulary:
                out.append("".join(words[i:end]))
                i = end
                break
        else:
            out.append(words[i])
            i += 1
    return out


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


def user_title_cores(title: str) -> tuple[str, ...]:
    """The requested title compared both with and without a trailing " - …"
    segment, since in the user's text that may be metadata or content (see
    split_metadata). Whichever reading fits a result better is the one used."""
    return tuple(dict.fromkeys((core_title(title), core_title(title, dash=False))))


def score_artist(want_artist: str, result_artists: list | None) -> tuple[float, float]:
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

    Extra artists on the result (guests, collaborators) never penalise it,
    metadata in a name ("Artist (UK)", "Artist - Topic") is ignored like in a
    title, and malformed entries are ignored.
    """
    return _artist_scores(_Request.of(want_artist, ""), _Result.of({"artists": result_artists}))


def artists_label(result: dict) -> str:
    """Human-readable artist string for a search result, for logs and dialogs."""
    return ", ".join(
        a.get("name") or ""
        for a in (result.get("artists") or [])
        if isinstance(a, dict)
    )


def validate_request(artist: str, title: str) -> list[str]:
    """Reasons this imported row can't be matched automatically (empty if fine).

    Checked before searching, so a hopeless row doesn't cost a network call.
    """
    reasons = []
    if not tokens(title):
        reasons.append("no title in the imported row")
    if not tokens(artist):
        reasons.append("no artist in the imported row")
    return reasons


def has_content_overlap(want_title: str, got_title: str | None) -> bool:
    """True when the titles share a word that actually says something.

    "The End" and "The Beginning" share "the", which is no evidence at all —
    and under an "Always add" policy that word alone could authorise a
    completely different song. Titles built entirely from stop words ("You and
    Me") fall back to any shared token, or they could never match anything.
    """
    return _related(_Request.of("", want_title), _Result.of({"title": got_title}))


def _cores_overlap(want_core, got_core):
    """has_content_overlap for one reading of the requested title."""
    want, got = (Counter(t) for t in align_spacing(tokens(want_core), tokens(got_core)))
    shared = set((want & got).elements())
    if shared - STOP_WORDS:
        return True
    if shared:  # only stop words in common — evidence only if that's all there is
        return not (set(want) - STOP_WORDS)
    # No shared token at all is normally decisive, but scripts without word
    # boundaries have only one token to begin with; there score_text falls back
    # to character similarity, and that score is the evidence.
    return _score_spaceless(" ".join(want.elements()), " ".join(got.elements())) > 0.0


@dataclass(frozen=True, slots=True)
class _Request:
    """The requested song, interpreted once. Every rule reads these values, so
    scoring, relatedness, version checks and explanations can't disagree about
    what the user asked for. The user's text keeps a trailing " - …" as one
    possible reading (see split_metadata)."""

    normalized: str             # the whole title, normalized
    cores: tuple[str, ...]      # comparable readings: user_title_cores
    markers: frozenset[str]     # version markers asked for
    principal: str              # principal artist: metadata stripped, no leading "the"
    guests: tuple[str, ...]     # featured artists, same treatment

    @classmethod
    def of(cls, artist: str, title: str) -> "_Request":
        # Metadata goes first: split_featured trims a trailing ")" and would
        # leave "Artist (UK" behind.
        principal, guests = split_featured(strip_metadata(artist, dash=False), allow_with=True)
        return cls(
            normalized=normalize(title),
            cores=user_title_cores(title),
            markers=frozenset(version_markers(title)),
            principal=_artist_text(principal),
            guests=tuple(_artist_text(g) for g in guests if tokens(g)),
        )


@dataclass(frozen=True, slots=True)
class _Result:
    """One search result, interpreted once — YouTube Music's text, where any
    bracket and a trailing " - …" are metadata. Tolerates every field missing."""

    normalized: str             # the whole title, normalized
    core: str                   # core_title: what is compared
    markers: frozenset[str]     # version markers on the recording
    names: tuple[str, ...]      # each credited artist, like _Request.principal
    all_names: str              # every artist as one credit ("Air, Phoenix")
    explicit: bool
    seconds: float | None       # length, None when YouTube Music didn't say

    @classmethod
    def of(cls, result: dict) -> "_Result":
        title = result.get("title")
        names = [strip_metadata(a.get("name")) for a in (result.get("artists") or [])
                 if isinstance(a, dict) and a.get("name")]
        return cls(
            normalized=normalize(title),
            core=core_title(title),
            markers=frozenset(version_markers(title)),
            names=tuple(_artist_text(n) for n in names),
            all_names=" ".join(names),
            explicit=bool(result.get("isExplicit")),
            seconds=_duration_seconds(result),
        )


def _artist_text(name: str) -> str:
    """"The Beatles" and "Beatles" name the same band."""
    return " ".join(strip_leading_the(tokens(name)))


def _title_score(req: _Request, res: _Result) -> float:
    """Score two titles 0.0–1.0, metadata and featured artists discounted.
    Identical normalized titles score 1.0 before anything is stripped."""
    if req.normalized == res.normalized:
        return 1.0
    return max(score_text(core, res.core) for core in req.cores)


def _artist_scores(req: _Request, res: _Result) -> tuple[float, float]:
    """(principal, combined); see score_artist."""
    if not res.names or not req.principal:
        return 0.0, 0.0

    def best(one):
        # The result may bundle every artist into one string ("Air, Phoenix").
        return max(score_text(one, name) for name in (*res.names, res.all_names))

    principal = best(req.principal)
    found_guests = sum(1 for guest in req.guests if best(guest) >= HIGH_ARTIST)
    return principal, min(1.0, principal + GUEST_BONUS * found_guests)


def _relation(req: _Request, res: _Result) -> str:
    """"same" / "extra" / "missing"; see version_relation."""
    if req.normalized == res.normalized:
        return "same"  # literally the same string; nothing to compare
    if res.markers - req.markers:
        return "extra"
    if req.markers - res.markers:
        return "missing"
    return "same"


def _related(req: _Request, res: _Result) -> bool:
    """has_content_overlap for prepared values."""
    return any(_cores_overlap(core, res.core) for core in req.cores)


@dataclass(slots=True, repr=False)
class Candidate:
    """One scored search result, with the reasons it isn't a perfect answer.

    Scoring lives here rather than in the UI so the review dialog can list
    alternatives without recomputing anything. Scores are keyword-only and all
    required: `principal_score` and the guest-boosted `artist_score` are easy
    to mix up, and only the principal may decide confidence.
    """

    result: dict
    _: KW_ONLY
    title_score: float
    principal_score: float  # the requested principal artist alone: decides confidence
    artist_score: float     # principal + guest bonus: for ranking only
    overall_score: float
    relation: str           # "same" | "missing" | "extra", see version_relation
    reasons: list[str] = field(default_factory=list)
    # The result as interpreted for matching; set by choose_match, and what
    # grouping reads. Absent on hand-built candidates (tests, the dialog).
    prepared: _Result | None = field(default=None, compare=False)

    @property
    def video_id(self) -> str | None:
        return (self.result or {}).get("videoId")

    @property
    def label(self) -> str:
        """"Artist — Title", for logs and the review dialog."""
        return f"{artists_label(self.result)} — {self.result.get('title') or ''}"

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def __repr__(self):
        return f"Candidate({self.label!r}, overall={self.overall_score:.2f})"


@dataclass(slots=True)
class MatchDecision:
    """What the evidence says about a requested song. Carries no policy."""

    status: str                         # "high" | "ambiguous" | "weak" | "rejected"
    candidate: Candidate | None = None  # the proposal, or None
    reasons: list[str] = field(default_factory=list)
    alternatives: list[Candidate] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """The reasons as one sentence, for logs and the review dialog."""
        return "; ".join(self.reasons)

    @property
    def video_id(self) -> str | None:
        return self.candidate.video_id if self.candidate else None

    @property
    def label(self) -> str:
        """"Artist — Title" of the proposed match, or "" when there is none."""
        return self.candidate.label if self.candidate else ""

    @property
    def choices(self) -> list[Candidate]:
        """The proposal followed by its alternatives, for the review dialog."""
        return ([self.candidate] if self.candidate else []) + list(self.alternatives)


def choose_match(artist: str, title: str, results: list | None) -> MatchDecision:
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

    req = _Request.of(artist, title)
    scored = [
        _score(req, result)
        for result in results or []
        if isinstance(result, dict) and result.get("videoId")  # else nothing to add
    ]
    if not scored:
        return MatchDecision("rejected", reasons=["no usable search results"])
    # Best first. On a tie the result titled exactly as requested leads, rather
    # than whichever variant YouTube Music happened to list first.
    scored.sort(key=lambda c: (c.overall_score, c.prepared.normalized == req.normalized),
                reverse=True)

    # Versions of the requested song by the requested artist decide on their
    # own: one recording is the answer, several are a choice. The same
    # recording listed twice (album and single) counts once, at its best rank.
    versions = _distinct_recordings([c for c in scored if _is_version(c)])
    others = [c for c in scored if not _is_version(c)]
    if versions:
        winner, *more = versions
        if not more:
            return MatchDecision("high", winner, alternatives=others[:3])
        # Every version is offered — the user may want several — topped up with
        # other candidates to the usual three alternatives.
        return MatchDecision("ambiguous", winner,
                             reasons=[f"{len(versions)} versions of this song found"],
                             alternatives=more + others[:max(0, 3 - len(more))])

    # No version: offer the best related song (a cover, a loose title match),
    # never confidently. Refused outright only when nothing shares a *content*
    # word with the title: "the" alone is no evidence, and under an "Always add"
    # policy it would authorise a different song unreviewed. This rejects
    # "One" → "Someone" and "Lisztomania" → "1901".
    related = [c for c in scored if _related(req, c.prepared)]
    if not related:
        return MatchDecision("rejected", reasons=["no result with a related title"])
    winner, *rest = related
    # "Weak" is evidence too thin to hand to an "Always add" policy: a loose
    # title overlap or a wholly different performer always asks.
    thin = winner.title_score < WEAK_TITLE or winner.principal_score < WEAK_ARTIST
    return MatchDecision("weak" if thin else "ambiguous", winner,
                         reasons=list(winner.reasons), alternatives=rest[:3])


def _score(req: _Request, result: dict) -> Candidate:
    """Score one search result against the requested song."""
    res = _Result.of(result)
    title_score = _title_score(req, res)
    principal_score, artist_score = _artist_scores(req, res)
    relation = _relation(req, res)
    base = TITLE_WEIGHT * title_score + ARTIST_WEIGHT * artist_score
    candidate = Candidate(
        result,
        title_score=title_score,
        principal_score=principal_score,
        artist_score=artist_score,
        overall_score=base * (1.0 - VERSION_PENALTY[relation]),
        relation=relation,
        prepared=res,
    )
    candidate.reasons = _shortfalls(candidate, req)
    return candidate


def _is_version(candidate: Candidate) -> bool:
    """The requested title by the requested principal artist — the scores have
    already discounted spacing, metadata and featured artists. The *principal*
    score is what's tested, never the guest-boosted one: a guest may help a
    candidate win, but it can't make it certain."""
    return candidate.title_score >= HIGH_TITLE and candidate.principal_score >= HIGH_ARTIST


@dataclass(slots=True)
class _Entry:
    rank: int                # position in the score ranking, 0 = best
    candidate: Candidate
    seconds: float | None    # length, None when YouTube Music didn't say


@dataclass(slots=True)
class _RecordingGroup:
    """Entries taken to be one recording: same title and explicit flag, and
    known lengths spanning at most SAME_RECORDING_SECONDS."""

    entries: list[_Entry]

    @property
    def best_rank(self) -> int:
        """Where the group ranks: its best-ranked member, whoever that is."""
        return min(e.rank for e in self.entries)

    @property
    def shortest(self) -> float:
        return min(e.seconds for e in self.entries if e.seconds is not None)

    @property
    def representative(self) -> Candidate:
        """What the group is shown and added as: its best-ranked member *with a
        known length*, since the length is what tells recordings apart. Not
        necessarily the member that decides best_rank."""
        known = [e for e in self.entries if e.seconds is not None]
        return min(known or self.entries, key=lambda e: e.rank).candidate


def _distinct_recordings(candidates: list[Candidate]) -> list[Candidate]:
    """One candidate per recording, best-ranked recording first.

    Candidates are partitioned by title and explicit flag, then grouped by
    length within each (_group_by_length). The outcome doesn't depend on the
    order YouTube Music lists results in.
    """
    tracks: dict[tuple[str, bool], list[_Entry]] = {}
    for rank, c in enumerate(candidates):
        key = (c.prepared.normalized, c.prepared.explicit)
        tracks.setdefault(key, []).append(_Entry(rank, c, c.prepared.seconds))
    groups = [g for entries in tracks.values() for g in _group_by_length(entries)]
    return [g.representative for g in sorted(groups, key=lambda g: g.best_rank)]


def _group_by_length(entries: list[_Entry]) -> list[_RecordingGroup]:
    """Group one track's entries into recordings.

    Known lengths are walked in sorted order and a group closes once a length
    is more than SAME_RECORDING_SECONDS past its shortest, so 3:00 → 3:10 →
    3:20 can't chain into one. Unknown lengths (YouTube Music omits them often
    enough that "unknown" must not create questions) then all join the
    best-ranked group — they can never bridge two.
    """
    groups: list[_RecordingGroup] = []
    for e in sorted((e for e in entries if e.seconds is not None), key=lambda e: e.seconds):
        if groups and e.seconds - groups[-1].shortest <= SAME_RECORDING_SECONDS:
            groups[-1].entries.append(e)
        else:
            groups.append(_RecordingGroup([e]))
    unknown = [e for e in entries if e.seconds is None]
    if unknown and groups:
        min(groups, key=lambda g: g.best_rank).entries.extend(unknown)
    elif unknown:
        groups.append(_RecordingGroup(unknown))
    return groups


def _duration_seconds(result: dict) -> float | None:
    """The result's length in seconds: `duration_seconds`, else "m:ss" or
    "h:mm:ss" parsed from `duration`, else None."""
    seconds = result.get("duration_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return seconds
    parts = str(result.get("duration") or "").split(":")
    if len(parts) in (2, 3) and all(p.strip().isdigit() for p in parts):
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total
    return None


def _shortfalls(candidate: Candidate, req: _Request) -> list[str]:
    """Every way this candidate falls short of being the obvious answer."""
    reasons = []
    if candidate.title_score < HIGH_TITLE:
        reasons.append(
            f"title similarity {candidate.title_score:.2f} below {HIGH_TITLE:.2f}"
        )
    if candidate.principal_score < HIGH_ARTIST:
        reasons.append(
            f"artist similarity {candidate.principal_score:.2f} below {HIGH_ARTIST:.2f}"
            f" (found {artists_label(candidate.result) or 'no artist'})"
        )
    if candidate.relation != "same":
        reasons.append(_version_reason(candidate.relation, req, candidate.prepared))
    return reasons


def _version_reason(relation: str, req: _Request, res: _Result) -> str:
    """Explain a recording-version difference in the user's terms."""
    want, got = req.markers, res.markers
    if relation == "missing":
        wanted = ", ".join(sorted(want - got))
        return f"no {wanted} version found — this is the standard recording"
    extra = ", ".join(sorted(got - want)) or "different"
    return f"this is the {extra} version, not the one asked for"


