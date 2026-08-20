"""Tests for the matching heuristic. No Tk, no network.

The whole point of this module is refusing to guess, so most of these tests are
about what must *not* be accepted.
"""

import pytest

from cratefill.matching import (
    HIGH_ARTIST,
    HIGH_TITLE,
    MIN_ARTIST,
    MIN_TITLE,
    choose_match,
    core_title,
    has_version_conflict,
    normalize,
    score_text,
    split_featured,
    tokens,
    validate_request,
    version_markers,
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

    def test_core_title_drops_markers_and_features(self):
        assert core_title("Wonderwall (Remastered)") == "wonderwall"
        assert core_title("This Is It (feat. Guest)") == "this is it"


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
        """"hello" must not pass as "hello world goodbye" just by being inside it."""
        assert score_text("hello", "hello world goodbye") < MIN_TITLE

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


class TestRejected:
    @pytest.mark.parametrize("label, artist, title, res", [
        ("one vs someone", "Cher", "One", result("Someone", "Cherub")),
        ("cher vs cherub", "Cher", "Believe", result("Believe", "Cherub")),
        ("same title, other artist", "Phoenix", "Lisztomania",
         result("Lisztomania", "Some Orchestra Ensemble")),
        ("right artist, wrong song", "Phoenix", "Lisztomania", result("1901", "Phoenix")),
        ("title inside another", "Adele", "Hello", result("Hello World Goodbye", "Adele")),
    ])
    def test_thresholds_cannot_compensate_each_other(self, label, artist, title, res):
        d = decide(artist, title, res)
        assert d.status == "rejected", f"{label}: {d}"
        assert d.title_score < MIN_TITLE or d.artist_score < MIN_ARTIST

    @pytest.mark.parametrize("title, got", [
        ("Wonderwall", "Wonderwall (Live at Wembley)"),
        ("Wonderwall (Live)", "Wonderwall"),
        ("One More Time", "One More Time (Radio Edit)"),
        ("One More Time", "One More Time (Skrillex Remix)"),
        ("Stronger", "Stronger (Instrumental)"),
        ("Wonderwall", "Wonderwall (Cover)"),
        ("Bad Habit", "Bad Habit (Sped Up)"),
        ("Bad Habit", "Bad Habit (Slowed)"),
        ("Stan", "Stan (Explicit)"),
    ])
    def test_a_different_recording_is_never_added(self, title, got):
        d = decide("Oasis", title, result(got, "Oasis"))
        assert d.status == "rejected"
        assert d.candidate is None, "a rejected decision must offer nothing to add"

    def test_no_usable_results(self):
        assert decide("Phoenix", "Lisztomania").status == "rejected"

    def test_result_without_video_id_is_not_a_candidate(self):
        d = decide("Phoenix", "Lisztomania",
                   {"title": "Lisztomania", "artists": [{"name": "Phoenix"}]})
        assert d.status == "rejected"

    def test_missing_artist_data_cannot_be_high_confidence(self):
        d = decide("Phoenix", "Lisztomania",
                   {"videoId": "v1", "title": "Lisztomania", "artists": None})
        assert d.status == "rejected"

    @pytest.mark.parametrize("artist, title", [("", "Lisztomania"), ("Phoenix", "")])
    def test_incomplete_imported_row(self, artist, title):
        d = decide(artist, title, result("Lisztomania", "Phoenix"))
        assert d.status == "rejected"
        assert "imported row" in d.reason

    def test_rejection_explains_itself(self):
        d = decide("Cher", "Believe", result("Believe", "Cherub"))
        assert "artist similarity" in d.reason


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

    def test_conflicting_versions_are_filtered_before_ranking(self):
        """A live result alongside the studio one must not make it ambiguous —
        it isn't a credible candidate at all."""
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall (Live)", "Oasis", vid="a"),
                   result("Wonderwall", "Oasis", vid="b"))
        assert d.status == "high" and d.video_id == "b"

    def test_alternatives_are_offered_for_review(self):
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall", "Oasis", vid="a"),
                   result("Wonderwall (Deluxe)", "Oasis", vid="b"))
        assert [a["videoId"] for a in d.alternatives] == ["b"]


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
        assert d.status in ("high", "ambiguous", "rejected"), label
        assert d.candidate is None or isinstance(d.candidate, dict), label
