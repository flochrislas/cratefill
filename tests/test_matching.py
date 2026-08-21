"""Tests for the matching heuristic. No Tk, no network.

The whole point of this module is refusing to guess, so most of these tests are
about what must *not* be accepted.
"""

import pytest

from cratefill.matching import (
    HIGH_ARTIST,
    HIGH_TITLE,
    choose_match,
    core_title,
    has_version_conflict,
    normalize,
    score_text,
    split_featured,
    tokens,
    validate_request,
    version_markers,
    version_relation,
)


def result(title, *artists, vid="v1"):
    """A ytmusicapi-shaped search result."""
    return {"videoId": vid, "title": title, "artists": [{"name": a} for a in artists]}


def decide(artist, title, *results):
    return choose_match(artist, title, list(results))


class TestNormalize:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Phoenix", "phoenix"),
            ("PHOENIX", "phoenix"),
            ("Phoenix!", "phoenix"),
            ("Harder, Better", "harder better"),
            ("  padded  ", "padded"),
            ("Beyoncé", "beyonce"),                    # accents stripped
            ("Étienne Daho", "etienne daho"),
            ("Cœur de pirate", "coeur de pirate"),     # ligature NFKD won't split
            ("Blue Öyster Cult", "blue oyster cult"),
            ("Mötley Crüe", "motley crue"),
            ("AC/DC", "ac dc"),                        # slash becomes a space
            ("P!nk", "pink"),                          # interior ! is a letter
            ("Ke$ha", "kesha"),
            ("Sigur Rós – Hoppípolla", "sigur ros hoppipolla"),  # whitespace collapses
            ("Sweet Child o’ Mine", "sweet child o mine"),       # curly apostrophe
            ("", ""),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize(raw) == expected

    def test_tolerates_none(self):
        """YT Music omits or nulls fields like title and artist name."""
        assert normalize(None) == ""

    def test_tokens_are_whole_words(self):
        assert tokens("Harder, Better!") == ["harder", "better"]


class TestFeatured:
    @pytest.mark.parametrize("raw", [
        "Calvin Harris feat. Rihanna",
        "Calvin Harris featuring Rihanna",
        "Calvin Harris ft. Rihanna",
        "Calvin Harris FEAT Rihanna",
    ])
    def test_splits_every_spelling(self, raw):
        assert split_featured(raw) == ("Calvin Harris", ["Rihanna"])

    def test_multiple_guests(self):
        main, guests = split_featured("DJ feat. A, B & C")
        assert (main, guests) == ("DJ", ["A", "B", "C"])

    def test_no_featured_part(self):
        assert split_featured("Phoenix") == ("Phoenix", [])


class TestVersionMarkers:
    @pytest.mark.parametrize("title, marker", [
        ("Wonderwall (Live at Wembley)", "live"),
        ("One More Time (Skrillex Remix)", "remix"),
        ("Layla (Acoustic)", "acoustic"),
        ("Stronger (Instrumental)", "instrumental"),
        ("Wonderwall (Karaoke Version)", "karaoke"),
        ("Hurt (Cover)", "cover"),
        ("Bad Habit (Sped Up)", "sped up"),
        ("Bad Habit (Slowed + Reverb)", "slowed"),
        ("Stan (Explicit)", "explicit"),
        ("Stan (Clean)", "clean"),
    ])
    def test_finds_hard_markers(self, title, marker):
        assert marker in version_markers(title)[0]

    @pytest.mark.parametrize("title", [
        "Wonderwall (Remastered)",
        "Come Together (2009 Remaster)",
        "Song (Deluxe Edition)",
        "Song (Album Version)",
        "Song (Official Video)",
    ])
    def test_soft_markers_are_not_hard(self, title):
        assert version_markers(title)[0] == set()

    def test_parenthesised_text_is_not_discarded(self):
        """Stripping brackets would throw away exactly the identifying detail."""
        assert version_markers("Wonderwall (Live)")[0] == {"live"}

    @pytest.mark.parametrize("want, got, conflict", [
        ("Wonderwall", "Wonderwall (Live)", True),
        ("Wonderwall (Live)", "Wonderwall", True),          # also the other direction
        ("Wonderwall (Live)", "Wonderwall (Live)", False),
        ("Wonderwall", "Wonderwall (Remastered)", False),   # soft marker only
        ("Wonderwall", "Wonderwall", False),
    ])
    def test_conflicts_both_ways(self, want, got, conflict):
        assert has_version_conflict(want, got) is conflict

    @pytest.mark.parametrize("want, got, relation", [
        ("Wonderwall", "Wonderwall", "same"),
        ("Wonderwall (Live)", "Wonderwall (Live)", "same"),
        ("Wonderwall", "Wonderwall (Remastered)", "same"),     # soft markers ignored
        ("Wonderwall", "Wonderwall (Live)", "extra"),          # not what was asked for
        ("Wonderwall (Live)", "Wonderwall", "missing"),        # the usable fallback
        ("Wonderwall (Live)", "Wonderwall (Remix)", "extra"),  # a different recording
        ("Wonderwall (Live)", "Wonderwall (Live Remix)", "extra"),
        ("Wonderwall (Live Acoustic)", "Wonderwall (Live)", "missing"),
    ])
    def test_relation_is_asymmetric(self, want, got, relation):
        """The two directions are not equally bad, so they are named separately."""
        assert version_relation(want, got) == relation

    @pytest.mark.parametrize("title, core", [
        ("Wonderwall", "wonderwall"),
        ("Wonderwall (Remastered)", "wonderwall"),
        ("This Is It (feat. Guest)", "this is it"),
        # A bracketed version group goes whole: keeping "at wembley" would make
        # the live take look like a different song and drag its score below an
        # unrelated band's studio cut.
        ("Wonderwall (Live at Wembley)", "wonderwall"),
        ("Wonderwall (Karaoke Version)", "wonderwall"),
        ("Bad Habit (Slowed + Reverb)", "bad habit"),
        ("One More Time (Skrillex Remix)", "one more time"),
        ("Hurt (Johnny Cash Cover)", "hurt"),
        ("Song (2009 Remaster) (Live)", "song"),
        # A bracketed group with no version marker is part of the title.
        ("Song (Reprise)", "song reprise"),
    ])
    def test_core_title(self, title, core):
        assert core_title(title) == core

    def test_a_live_recording_scores_as_the_same_song(self):
        """The venue text must not cost the candidate title similarity — the
        version difference is reported separately."""
        d = decide("Oasis", "Wonderwall", result("Wonderwall (Live at Wembley)", "Oasis"))
        assert d.title_score == 1.0
        assert d.reason == "this is the live version, not the one asked for"


class TestScoreText:
    def test_identical(self):
        assert score_text("wonderwall", "wonderwall") == 1.0

    def test_word_order_is_irrelevant(self):
        assert score_text("hello world", "world hello") == 1.0

    @pytest.mark.parametrize("want, got", [
        ("one", "someone"),      # the substring bug this replaces
        ("cher", "cherub"),
        ("love", "lovely"),
    ])
    def test_no_credit_without_a_shared_whole_word(self, want, got):
        assert score_text(want, got) == 0.0

    def test_extra_words_are_penalised_symmetrically(self):
        """"hello" must not score as highly as an exact hit just by being inside
        "hello world goodbye"."""
        assert score_text("hello", "hello world goodbye") < HIGH_TITLE

    def test_missing_side_scores_zero(self):
        assert score_text("", "wonderwall") == 0.0
        assert score_text("wonderwall", None) == 0.0


class TestValidateRequest:
    def test_accepts_a_complete_row(self):
        assert validate_request("Phoenix", "Lisztomania") == []

    def test_reports_what_is_missing(self):
        assert "no title in the imported row" in validate_request("Phoenix", "")
        assert "no artist in the imported row" in validate_request("", "Lisztomania")


class TestHighConfidence:
    @pytest.mark.parametrize("label, artist, title, res", [
        ("exact", "Phoenix", "Lisztomania", result("Lisztomania", "Phoenix")),
        ("accents", "Beyonce", "Halo", result("Halo", "Beyoncé")),
        ("ligature", "Cœur de pirate", "Comme des enfants",
         result("Comme des enfants", "Coeur de pirate")),
        ("punctuation", "AC/DC", "Highway to Hell", result("Highway to Hell", "AC DC")),
        ("stylised", "P!nk", "Just Give Me a Reason", result("Just Give Me a Reason", "Pink")),
        ("leading the", "Beatles", "Come Together", result("Come Together", "The Beatles")),
        ("the on the result", "The Beatles", "Come Together", result("Come Together", "Beatles")),
        ("featured artist kept", "Calvin Harris feat. Rihanna", "This Is What You Came For",
         result("This Is What You Came For", "Calvin Harris", "Rihanna")),
        ("extra artist on result", "Calvin Harris", "This Is What You Came For",
         result("This Is What You Came For", "Calvin Harris", "Rihanna")),
        ("featured in the title", "Calvin Harris", "This Is What You Came For",
         result("This Is What You Came For (feat. Rihanna)", "Calvin Harris")),
        ("remastered", "Oasis", "Wonderwall", result("Wonderwall (Remastered)", "Oasis")),
        ("year remaster", "Beatles", "Come Together",
         result("Come Together (2009 Remaster)", "The Beatles")),
        ("album version", "Guns N' Roses", "Sweet Child o' Mine",
         result("Sweet Child O' Mine (Album Version)", "Guns N’ Roses")),
        ("comma punctuation", "Daft Punk", "Harder Better Faster Stronger",
         result("Harder, Better, Faster, Stronger", "Daft Punk")),
        ("live matches live", "Oasis", "Wonderwall (Live)", result("Wonderwall (Live)", "Oasis")),
    ])
    def test_added_automatically(self, label, artist, title, res):
        d = decide(artist, title, res)
        assert d.status == "high", f"{label}: {d}"
        assert d.video_id == "v1"
        assert d.title_score >= HIGH_TITLE and d.artist_score >= HIGH_ARTIST


class TestExactMatchInvariant:
    """The one property that must never break: an exact result is a match.

    Title preprocessing once reduced "With or Without You" and "Clean" to empty
    strings, so songs whose titles *are* metadata words were rejected outright.
    Every marker word is tried as a whole title here, so a future addition to the
    marker lists can't quietly resurrect that.
    """

    MARKER_TITLES = [
        "Clean", "Explicit", "Live", "Live and Let Die", "Cover Me", "Karaoke",
        "Demo", "Acoustic", "Instrumental", "Radio", "Remix", "Mono", "Stereo",
        "Deluxe", "Remaster", "Anniversary", "Extended", "Slowed", "Reverb",
        "Sped Up", "Nightcore", "Tribute", "Unplugged", "Audio", "Visualizer",
        "The Cover", "Album", "Single", "Bonus Track", "Original Version",
    ]
    TRICKY_TITLES = [
        "With or Without You", "Dancing with Myself", "Live with Me",
        "Feat. Nobody", "Sitting with the Devil", "Mono No Aware",
    ]

    @pytest.mark.parametrize("title", MARKER_TITLES + TRICKY_TITLES)
    def test_an_exact_result_is_always_high(self, title):
        d = decide("Some Artist", title, result(title, "Some Artist"))
        assert d.status == "high", f"{title!r} → {d} (core {core_title(title)!r})"
        assert d.title_score == 1.0

    @pytest.mark.parametrize("title", MARKER_TITLES + TRICKY_TITLES)
    def test_the_core_title_is_never_empty(self, title):
        """The backstop: if stripping metadata would empty the title, the
        metadata *was* the title."""
        assert core_title(title) != ""

    @pytest.mark.parametrize("title, got", [
        ("Dancing with Myself", "Dancing"),
        ("With or Without You", "You"),
        ("Live and Let Die", "Live"),
        ("Sitting with the Devil", "Sitting"),
    ])
    def test_dropping_a_real_word_cannot_be_confident(self, title, got):
        """Preprocessing that erased a meaningful word made two different songs
        look identical. Whatever survives stripping, this must never be `high`."""
        d = decide("Some Artist", title, result(got, "Some Artist"))
        assert d.status != "high", f"{title!r} vs {got!r} → {d}"


class TestPrincipalArtist:
    """A featured guest can help, never stand in for the principal artist."""

    def test_all_credited_artists_present(self):
        d = decide("Jay-Z feat. Alicia Keys", "Empire State of Mind",
                   result("Empire State of Mind", "Jay-Z", "Alicia Keys"))
        assert d.status == "high"

    def test_principal_alone_is_enough(self):
        """A result that omits the guest is still the right recording."""
        d = decide("Jay-Z feat. Alicia Keys", "Empire State of Mind",
                   result("Empire State of Mind", "Jay-Z"))
        assert d.status == "high"

    def test_a_guest_only_result_is_not_confident(self):
        """The guest matching perfectly used to score 1.00 and hide the fact that
        the principal artist wasn't there at all."""
        d = decide("Jay-Z feat. Alicia Keys", "Empire State of Mind",
                   result("Empire State of Mind", "Alicia Keys"))
        assert d.status != "high"
        assert d.artist_score < HIGH_ARTIST
        assert "Alicia Keys" in d.reason

    def test_a_guest_cannot_lift_a_wrong_principal_over_the_bar(self):
        d = decide("Jay-Z feat. Alicia Keys", "Empire State of Mind",
                   result("Empire State of Mind", "Alicia Keys", "Someone Else"))
        assert d.artist_score < HIGH_ARTIST

    def test_with_is_a_separator_in_artists(self):
        """Unlike titles, artist credits really do use "with"."""
        d = decide("Ella Fitzgerald with Louis Armstrong", "Cheek to Cheek",
                   result("Cheek to Cheek", "Ella Fitzgerald", "Louis Armstrong"))
        assert d.status == "high"


class TestRepeatedWords:
    def test_multiplicity_matters(self):
        """Token *sets* made "Run Run Run" and "Run" identical."""
        assert score_text("run run run", "run") < 1.0

    def test_a_shortened_repetition_is_not_confident(self):
        d = decide("Jo Jo Gunne", "Run Run Run", result("Run", "Jo Jo Gunne"))
        assert d.status != "high"

    def test_it_is_still_offered(self):
        """Not confident, but plausibly the same song — don't refuse it."""
        d = decide("Jo Jo Gunne", "Run Run Run", result("Run", "Jo Jo Gunne"))
        assert d.status in OFFERED

    def test_reordering_with_equal_counts_is_still_exact(self):
        assert score_text("hello world hello", "hello hello world") == 1.0


class TestWeakTier:
    """Offered, but never automated: `weak` always asks, whatever the policy."""

    @pytest.mark.parametrize("label, artist, title, res", [
        ("loose title overlap", "Adele", "Hello", result("Hello World Goodbye", "Adele")),
        ("different song, shared words", "Simon", "The Sound of Silence",
         result("The Sound of Music", "Simon")),
        ("another band entirely", "Oasis", "Wonderwall", result("Wonderwall", "Tribute Players")),
        ("no artist data", "Oasis", "Wonderwall",
         {"videoId": "v1", "title": "Wonderwall", "artists": None}),
    ])
    def test_thin_evidence_is_weak(self, label, artist, title, res):
        d = decide(artist, title, res)
        assert d.status == "weak", f"{label}: {d}"
        assert d.video_id, "still offered — just never without asking"

    def test_a_solid_shortfall_stays_merely_ambiguous(self):
        """A near-tie between two good candidates is uncertain, not thin."""
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall", "Oasis", vid="a"),
                   result("Wonderwall (Deluxe)", "Oasis", vid="b"))
        assert d.status == "ambiguous"


class TestStopWordFloor:
    @pytest.mark.parametrize("title, got", [
        ("The End", "The Beginning"),
        ("A Day in the Life", "A Night at the Opera"),
        ("Sound of Silence", "Taste of Water"),
    ])
    def test_sharing_only_common_words_is_not_a_match(self, title, got):
        """Under an "Always add" policy, matching on "the" or "of" alone could
        authorise a completely different song without review."""
        d = decide("Some Artist", title, result(got, "Some Artist"))
        assert d.status == "rejected", f"{title!r} vs {got!r} → {d}"

    def test_a_title_made_only_of_common_words_still_matches_itself(self):
        d = decide("Some Artist", "You and Me", result("You and Me", "Some Artist"))
        assert d.status == "high"

    def test_an_all_common_word_title_falls_back_to_any_shared_word(self):
        """"You and Me" has no content word to require, so the floor relaxes —
        but the result is weak, so it still can't be added without asking."""
        d = decide("Some Artist", "You and Me", result("You and Her", "Some Artist"))
        assert d.status == "weak"


class TestSpacelessScripts:
    """Scripts without word boundaries have one token, so the whole-token gate
    can never find an overlap — a character fallback covers them."""

    def test_a_kana_variant_is_offered(self):
        d = decide("Angela Aki", "愛をこめて花束を", result("愛を込めて花束を", "Angela Aki"))
        assert d.status in OFFERED
        assert d.title_score >= 0.80

    def test_an_exact_japanese_title_is_confident(self):
        d = decide("宇多田ヒカル", "初恋", result("初恋", "宇多田ヒカル"))
        assert d.status == "high"

    def test_a_genuinely_different_japanese_title_is_refused(self):
        d = decide("Angela Aki", "愛をこめて花束を", result("手紙", "Angela Aki"))
        assert d.status == "rejected"

    @pytest.mark.parametrize("want, got", [
        ("one", "someone"),
        ("cher", "cherub"),
        ("hello", "hellos"),
    ])
    def test_latin_titles_do_not_get_the_fallback(self, want, got):
        """The gate is script-based precisely so this can't come back."""
        assert score_text(want, got) == 0.0


class TestRejected:
    """Rejection is reserved for a *different song*. Coming back empty-handed is
    the worst outcome, so anything recognisably the same song is offered instead
    — see TestOfferedRatherThanNothing."""

    @pytest.mark.parametrize("label, artist, title, res", [
        ("one vs someone", "Cher", "One", result("Someone", "Cherub")),
        ("right artist, wrong song", "Phoenix", "Lisztomania", result("1901", "Phoenix")),
        ("unrelated entirely", "Adele", "Hello", result("Bohemian Rhapsody", "Queen")),
    ])
    def test_no_shared_title_word_means_no_match(self, label, artist, title, res):
        d = decide(artist, title, res)
        assert d.status == "rejected", f"{label}: {d}"
        assert d.candidate is None, "a rejected decision must offer nothing to add"
        assert "related title" in d.reason

    def test_no_usable_results(self):
        assert decide("Phoenix", "Lisztomania").status == "rejected"

    def test_result_without_video_id_is_not_a_candidate(self):
        d = decide("Phoenix", "Lisztomania",
                   {"title": "Lisztomania", "artists": [{"name": "Phoenix"}]})
        assert d.status == "rejected"

    @pytest.mark.parametrize("artist, title", [("", "Lisztomania"), ("Phoenix", "")])
    def test_incomplete_imported_row(self, artist, title):
        d = decide(artist, title, result("Lisztomania", "Phoenix"))
        assert d.status == "rejected"
        assert "imported row" in d.reason


OFFERED = ("ambiguous", "weak")  # both reach the user; neither is added silently


class TestOfferedRatherThanNothing:
    """The whole point: a remix, a live take or another band's cover of the right
    song beats a silent miss. None of these may be *confident* — they land in the
    ambiguous or weak bucket, where the user sees them."""

    @pytest.mark.parametrize("title, got, expected_in_reason", [
        ("Wonderwall", "Wonderwall (Live at Wembley)", "live version"),
        ("One More Time", "One More Time (Radio Edit)", "radio edit version"),
        ("One More Time", "One More Time (Skrillex Remix)", "remix version"),
        ("Stronger", "Stronger (Instrumental)", "instrumental version"),
        ("Wonderwall", "Wonderwall (Cover)", "cover version"),
        ("Bad Habit", "Bad Habit (Sped Up)", "sped up version"),
        ("Stan", "Stan (Explicit)", "explicit version"),
    ])
    def test_a_different_recording_is_offered_not_refused(self, title, got, expected_in_reason):
        d = decide("Oasis", title, result(got, "Oasis"))
        assert d.status in OFFERED
        assert d.video_id == "v1", "the user should get the chance to take it"
        assert expected_in_reason in d.reason

    def test_another_artists_cover_is_offered(self):
        """A cover is by definition someone else, so a wrong artist can't be a
        hard refusal — it just can't be confident, and it can't be automated."""
        d = decide("Oasis", "Wonderwall", result("Wonderwall", "Tribute Players"))
        assert d.status == "weak"
        assert d.video_id == "v1"
        assert "Tribute Players" in d.reason

    def test_a_partial_title_overlap_is_offered(self):
        d = decide("Adele", "Hello", result("Hello World Goodbye", "Adele"))
        assert d.status in OFFERED

    def test_missing_artist_data_is_offered_but_never_confident(self):
        d = decide("Phoenix", "Lisztomania",
                   {"videoId": "v1", "title": "Lisztomania", "artists": None})
        assert d.status in OFFERED
        assert "no artist" in d.reason

    def test_the_real_artist_beats_another_bands_exact_version(self):
        """Version fidelity must not outweigh being the right performer: the
        penalty is folded into the score rather than used to exclude."""
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall", "Tribute Players", vid="cover"),
                   result("Wonderwall (Live)", "Oasis", vid="oasis-live"))
        assert d.video_id == "oasis-live"

    def test_every_shortfall_is_named(self):
        d = decide("Oasis", "Wonderwall", result("Wonderwall (Live)", "Tribute Players"))
        assert d.status in OFFERED
        assert "artist similarity" in d.reason and "live version" in d.reason


class TestVersionFallback:
    """Asking for a specific recording and only finding the standard one.

    Deliberately asymmetric: getting the album version when the live one isn't
    on YouTube Music beats getting nothing, so it is offered rather than
    refused — but never silently, so it can only ever be ambiguous.
    """

    @pytest.mark.parametrize("asked, marker", [
        ("Wonderwall (Live)", "live"),
        ("Wonderwall (Acoustic)", "acoustic"),
        ("Wonderwall (Remix)", "remix"),
        ("Wonderwall (Instrumental)", "instrumental"),
    ])
    def test_the_standard_recording_is_offered(self, asked, marker):
        d = decide("Oasis", asked, result("Wonderwall", "Oasis"))
        assert d.status == "ambiguous"
        assert d.video_id == "v1"
        assert f"no {marker} version found" in d.reason

    def test_the_exact_version_wins_when_it_exists(self):
        """A fallback must never outrank the recording that was actually asked
        for, nor make it look like a near tie."""
        for order in ([("Wonderwall", "album"), ("Wonderwall (Live)", "live")],
                      [("Wonderwall (Live)", "live"), ("Wonderwall", "album")]):
            d = decide("Oasis", "Wonderwall (Live)",
                       *[result(t, "Oasis", vid=v) for t, v in order])
            assert d.status == "high", d
            assert d.video_id == "live"

    def test_the_fallback_is_still_listed_as_an_alternative(self):
        d = decide("Oasis", "Wonderwall (Live)",
                   result("Wonderwall (Live)", "Oasis", vid="live"),
                   result("Wonderwall", "Oasis", vid="album"))
        assert [a.video_id for a in d.alternatives] == ["album"]

    def test_a_partial_marker_match_is_still_a_fallback(self):
        d = decide("Oasis", "Wonderwall (Live Acoustic)",
                   result("Wonderwall (Live)", "Oasis"))
        assert d.status == "ambiguous"
        assert "no acoustic version found" in d.reason

    @pytest.mark.parametrize("asked, offered, marker", [
        ("Wonderwall (Live)", "Wonderwall (Remix)", "remix"),
        ("Wonderwall (Acoustic)", "Wonderwall (Karaoke)", "karaoke"),
    ])
    def test_a_wrongly_marked_version_is_still_offered(self, asked, offered, marker):
        """Asking for live and being handed a remix isn't what was wanted, but
        it is the same song — so it's offered, with the mismatch named."""
        d = decide("Oasis", asked, result(offered, "Oasis"))
        assert d.status == "ambiguous"
        assert f"this is the {marker} version" in d.reason

    def test_the_standard_recording_outranks_a_wrongly_marked_one(self):
        """Both are imperfect, but being handed the plain recording is a milder
        disappointment than being handed a remix nobody asked for."""
        d = decide("Oasis", "Wonderwall (Live)",
                   result("Wonderwall (Remix)", "Oasis", vid="remix"),
                   result("Wonderwall", "Oasis", vid="standard"))
        assert d.video_id == "standard"

    def test_the_mismatch_names_the_unwanted_marker(self):
        d = decide("Oasis", "Wonderwall", result("Wonderwall (Karaoke)", "Oasis"))
        assert "this is the karaoke version, not the one asked for" in d.reason


class TestAmbiguity:
    def test_a_clear_winner_stays_high(self):
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall", "Oasis", vid="a"),
                   result("Champagne Supernova", "Oasis", vid="b"))
        assert d.status == "high" and d.video_id == "a"

    def test_a_near_tie_is_ambiguous(self):
        """Two catalogue entries for the same song: pick neither silently."""
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall", "Oasis", vid="a"),
                   result("Wonderwall (Deluxe)", "Oasis", vid="b"))
        assert d.status == "ambiguous"
        assert d.runner_up_score is not None
        assert "almost the same" in d.reason

    def test_the_version_penalty_keeps_a_variant_from_looking_like_a_tie(self):
        """A live result alongside the studio one is still a candidate — nothing
        is filtered — but VERSION_PENALTY drops it far enough that the studio
        version stays a clear winner rather than a near tie."""
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall (Live)", "Oasis", vid="a"),
                   result("Wonderwall", "Oasis", vid="b"))
        assert d.status == "high" and d.video_id == "b"
        assert d.runner_up_score is not None, "the live take is ranked, not discarded"
        assert d.alternatives[0].video_id == "a"

    def test_alternatives_are_offered_for_review(self):
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall", "Oasis", vid="a"),
                   result("Wonderwall (Deluxe)", "Oasis", vid="b"))
        assert [a.video_id for a in d.alternatives] == ["b"]


class TestMalformedApiData:
    @pytest.mark.parametrize("results, label", [
        (None, "results is None"),
        ([{"videoId": "v1", "title": "Lisztomania", "artists": None}], "artists=None"),
        ([{"videoId": "v1", "title": "Lisztomania"}], "artists key missing"),
        ([{"videoId": "v1", "title": "Lisztomania", "artists": [{}]}], "artist has no name"),
        ([{"videoId": "v1", "title": "Lisztomania", "artists": [{"name": None}]}], "name None"),
        ([{"videoId": "v1", "title": "Lisztomania", "artists": [None]}], "artist entry None"),
        ([{"videoId": "v1", "artists": [{"name": "Phoenix"}]}], "title key missing"),
        (["junk", {"videoId": "v1", "title": "Lisztomania"}], "non-dict result"),
        ([{"videoId": None, "title": "Lisztomania"}], "videoId None"),
    ])
    def test_never_raises(self, results, label):
        """ytmusicapi is unofficial: every field is optional. An exception here
        used to kill the whole worker thread."""
        d = choose_match("Phoenix", "Lisztomania", results)
        assert d.status in ("high", "ambiguous", "weak", "rejected"), label
        assert d.candidate is None or d.candidate.video_id, label
