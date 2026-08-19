"""Tests for the pure matching heuristic. No Tk, no network."""

import pytest

from cratefill.matching import normalize, pick_match


def song(video_id="v1", title="Lisztomania", artists=("Phoenix",)):
    """A ytmusicapi-shaped search result."""
    return {
        "videoId": video_id,
        "title": title,
        "artists": None if artists is None else [{"name": a} for a in artists],
    }


class TestNormalize:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Phoenix", "phoenix"),
            ("PHOENIX", "phoenix"),
            ("Phoenix!", "phoenix"),
            ("Harder, Better", "harder better"),
            ("  padded  ", "padded"),
            ("Étienne", "étienne"),          # accents survive: they are alphanumeric
            # Dropped punctuation leaves its surrounding spaces behind — internal
            # whitespace is not collapsed. Fine for substring matching; the
            # token-aware rewrite is where this would start to matter.
            ("Sigur Rós – Hoppípolla", "sigur rós  hoppípolla"),
            ("", ""),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize(raw) == expected

    def test_tolerates_none(self):
        """YT Music omits or nulls fields like title and artist name."""
        assert normalize(None) == ""


class TestPickMatch:
    def test_exact_match_is_confident(self):
        match, confident = pick_match([song()], "Phoenix", "Lisztomania")
        assert (match["videoId"], confident) == ("v1", True)

    def test_substring_titles_match(self):
        results = [song(title="Lisztomania (Radio Edit)")]
        assert pick_match(results, "Phoenix", "Lisztomania")[1] is True

    def test_prefers_first_confident_over_earlier_mismatch(self):
        results = [
            song("wrong", "Something Else", ("Nobody",)),
            song("right", "Lisztomania", ("Phoenix",)),
        ]
        match, confident = pick_match(results, "Phoenix", "Lisztomania")
        assert (match["videoId"], confident) == ("right", True)

    def test_falls_back_to_first_candidate_when_nothing_matches(self):
        results = [song("first", "Totally Other", ("Nobody",))]
        match, confident = pick_match(results, "Phoenix", "Lisztomania")
        assert (match["videoId"], confident) == ("first", False)

    def test_artist_is_optional_in_the_request(self):
        """A CSV row can carry a title only; the title alone then decides."""
        assert pick_match([song()], "", "Lisztomania")[1] is True

    def test_no_candidates(self):
        assert pick_match([], "Phoenix", "Lisztomania") == (None, False)

    def test_results_without_video_id_are_not_candidates(self):
        results = [song(video_id=None), {"title": "Lisztomania"}]
        assert pick_match(results, "Phoenix", "Lisztomania") == (None, False)

    def test_empty_title_is_never_a_confident_match(self):
        """"" is a substring of everything — a titleless result must not win."""
        match, confident = pick_match([song(title="")], "Phoenix", "Lisztomania")
        assert (match["videoId"], confident) == ("v1", False)

    @pytest.mark.parametrize(
        "results, label",
        [
            (None, "results is None"),
            ([{"videoId": "v1", "title": "Lisztomania", "artists": None}], "artists=None"),
            ([{"videoId": "v1", "title": "Lisztomania"}], "artists key missing"),
            ([{"videoId": "v1", "title": "Lisztomania", "artists": [{}]}], "artist has no name"),
            ([{"videoId": "v1", "title": "Lisztomania", "artists": [{"name": None}]}], "name None"),
            ([{"videoId": "v1", "title": "Lisztomania", "artists": [None]}], "artist entry None"),
            ([{"videoId": "v1", "artists": [{"name": "Phoenix"}]}], "title key missing"),
            (["junk", {"videoId": "v1", "title": "Lisztomania"}], "non-dict result"),
        ],
    )
    def test_survives_malformed_api_data(self, results, label):
        """ytmusicapi is unofficial: every field is optional. This must not raise
        — an exception here used to kill the whole worker thread."""
        match, confident = pick_match(results, "Phoenix", "Lisztomania")
        assert match is None or isinstance(match, dict), label
        assert isinstance(confident, bool), label
