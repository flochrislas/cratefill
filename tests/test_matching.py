"""Tests for the matching heuristic. No Tk, no network.

The whole point of this module is refusing to guess, so most of these tests are
about what must *not* be accepted.
"""

import itertools

import pytest

from cratefill.matching import (
    HIGH_ARTIST,
    HIGH_TITLE,
    choose_match,
    core_title,
    strip_metadata,
    normalize,
    score_artist,
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
        assert marker in version_markers(title)

    @pytest.mark.parametrize("title", [
        "Wonderwall (Remastered)",
        "Come Together (2009 Remaster)",
        "Song (Deluxe Edition)",
        "Song (Album Version)",
        "Song (Official Video)",
    ])
    def test_packaging_details_are_not_versions(self, title):
        assert version_markers(title) == set()

    def test_parenthesised_text_is_not_discarded(self):
        """Stripping brackets would throw away exactly the identifying detail."""
        assert version_markers("Wonderwall (Live)") == {"live"}

    @pytest.mark.parametrize("want, got, relation", [
        ("Wonderwall", "Wonderwall", "same"),
        ("Wonderwall (Live)", "Wonderwall (Live)", "same"),
        ("Wonderwall", "Wonderwall (Remastered)", "same"),     # packaging only
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
        # Any bracketed group goes, marker or not: brackets never count.
        ("Song (Pt. 2)", "song"),
        ("Song - From the Movie X", "song"),
    ])
    def test_core_title(self, title, core):
        assert core_title(title) == core

    def test_a_live_recording_scores_as_the_same_song(self):
        """The venue text must not cost the candidate title similarity — the
        version difference is reported separately."""
        d = decide("Oasis", "Wonderwall", result("Wonderwall (Live at Wembley)", "Oasis"))
        assert d.candidate.title_score == 1.0
        assert d.candidate.reason == "this is the live version, not the one asked for"


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
        ("radio edit", "Daft Punk", "One More Time",
         result("One More Time (Radio Edit)", "Daft Punk")),
        ("radio version", "Daft Punk", "One More Time",
         result("One More Time (Radio Version)", "Daft Punk")),
        ("comma punctuation", "Daft Punk", "Harder Better Faster Stronger",
         result("Harder, Better, Faster, Stronger", "Daft Punk")),
        ("live matches live", "Oasis", "Wonderwall (Live)", result("Wonderwall (Live)", "Oasis")),
    ])
    def test_added_automatically(self, label, artist, title, res):
        d = decide(artist, title, res)
        assert d.status == "high", f"{label}: {d}"
        assert d.video_id == "v1"
        assert d.candidate.title_score >= HIGH_TITLE and d.candidate.artist_score >= HIGH_ARTIST


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
        assert d.candidate.title_score == 1.0

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


class TestSpacing:
    """A missing or extra space is a spelling difference, not another name."""

    @pytest.mark.parametrize("artist, got", [
        ("ArtistName", "Artist Name"),
        ("Artist Name", "ArtistName"),
        ("AC/DC", "ACDC"),
        ("Daft Punk", "DaftPunk"),
    ])
    def test_artist_spacing_is_high(self, artist, got):
        d = decide(artist, "Some Song", result("Some Song", got))
        assert d.status == "high", d

    @pytest.mark.parametrize("title, got", [
        ("Love Song", "Lovesong"),
        ("Somebody", "Some Body"),
        ("Love Song Forever", "Lovesong Forever"),
    ])
    def test_title_spacing_is_high(self, title, got):
        d = decide("Some Artist", title, result(got, "Some Artist"))
        assert d.status == "high", d

    def test_spacing_is_forgiven_alongside_another_difference(self):
        """Re-joining split words must survive extra words on the result."""
        d = decide("Some Artist", "Love Song", result("Lovesong Radio Edit", "Some Artist"))
        assert d.status in OFFERED and d.video_id == "v1"

    @pytest.mark.parametrize("want, got", [("one", "someone"), ("cher", "cherub")])
    def test_joining_words_cannot_create_a_substring_match(self, want, got):
        """Words are only re-joined into a word the *other* side contains."""
        assert score_text(want, got) == 0.0


class TestUnrequestedSuffix:
    """A bracket or dash suffix the request didn't have costs nothing, unless it
    names a different recording."""

    @pytest.mark.parametrize("got", [
        "Song Name (From the Movie X)",
        'Song Name - From "Movie X"',
        "Song Name (Pt. 2)",
        "Song Name [Bonus]",
        "Song Name (Original Mix)",
    ])
    def test_extra_suffix_is_high(self, got):
        d = decide("Some Artist", "Song Name", result(got, "Some Artist"))
        assert d.status == "high", d

    @pytest.mark.parametrize("got", [
        "Song Name (Live)", "Song Name (Reprise)", "Song Name (Club Mix)",
        "Song Name (Medley)",
    ])
    def test_the_only_version_is_added_whatever_its_brackets_say(self, got):
        """With nothing else to choose from, asking is just an extra click."""
        d = decide("Some Artist", "Song Name", result(got, "Some Artist"))
        assert d.status == "high", d
        assert d.reasons == []

    @pytest.mark.parametrize("got", [
        "Song Name (Live)", "Song Name (Deluxe)", "Song Name (From the Movie X)",
        "Song Name (Radio Edit)",
    ])
    def test_several_versions_ask(self, got):
        """The user may want one of them, or several — whatever order YouTube
        Music listed them in, and even when one is the plain title."""
        for order in ([("Song Name", "a"), (got, "b")], [(got, "b"), ("Song Name", "a")]):
            d = decide("Some Artist", "Song Name",
                       *[result(t, "Some Artist", vid=v) for t, v in order])
            assert d.status == "ambiguous", (order, d)
            assert "2 versions of this song found" in d.reason
            assert {c.video_id for c in d.choices} == {"a", "b"}

    def test_identical_titles_are_one_version(self):
        """The same song on the album and on the single is not a choice."""
        d = decide("Some Artist", "Song Name",
                   result("Song Name", "Some Artist", vid="album"),
                   result("Song Name", "Some Artist", vid="single"))
        assert d.status == "high", d

    @pytest.mark.parametrize("a, b", [
        ({"isExplicit": True}, {"isExplicit": False}),
        ({"duration": "4:18"}, {"duration": "5:02"}),
        ({"duration_seconds": 258}, {"duration_seconds": 300}),
    ])
    def test_identical_titles_that_differ_visibly_ask(self, a, b):
        """Clean vs explicit, or a clearly different length, is a real choice
        even when the titles are identical."""
        d = decide("Some Artist", "Song Name",
                   dict(result("Song Name", "Some Artist", vid="one"), **a),
                   dict(result("Song Name", "Some Artist", vid="two"), **b))
        assert d.status == "ambiguous", d
        assert {c.video_id for c in d.choices} == {"one", "two"}

    @pytest.mark.parametrize("a, b", [
        ({"duration": "4:18", "isExplicit": False}, {"duration": "4:22", "isExplicit": False}),
        ({"duration": "4:18"}, {}),                       # unknown length isn't a difference
        ({"duration": "1:04:18"}, {"duration_seconds": 3858}),
    ])
    def test_identical_titles_that_look_the_same_are_one_version(self, a, b):
        d = decide("Some Artist", "Song Name",
                   dict(result("Song Name", "Some Artist", vid="one"), **a),
                   dict(result("Song Name", "Some Artist", vid="two"), **b))
        assert d.status == "high", d
        assert d.video_id == "one"

    @pytest.mark.parametrize("order", [("u", "a", "b"), ("a", "b", "u"), ("a", "u", "b")])
    def test_an_unknown_length_cannot_hide_a_known_difference(self, order):
        """3:00 and 6:00 are two recordings whatever position the unknown-length
        result comes in — it may join one of them, never merge them."""
        durations = {"u": None, "a": "3:00", "b": "6:00"}
        results = []
        for vid in order:
            r = result("Song Name", "Some Artist", vid=vid)
            if durations[vid]:
                r["duration"] = durations[vid]
            results.append(r)
        d = decide("Some Artist", "Song Name", *results)
        assert d.status == "ambiguous", (order, d)
        assert {"a", "b"} <= {c.video_id for c in d.choices}
        assert "2 versions of this song found" in d.reason

    @pytest.mark.parametrize("order", list(itertools.permutations(["3:00", "3:10", "3:20"])))
    def test_lengths_cannot_chain_across_more_than_the_tolerance(self, order):
        """3:00 and 3:20 are twenty seconds apart: whatever the order, two
        recordings — 3:10 bridging them must not merge all three."""
        d = decide("Some Artist", "Song Name",
                   *[dict(result("Song Name", "Some Artist", vid=x), duration=x) for x in order])
        assert d.status == "ambiguous", (order, d)
        assert "2 versions of this song found" in d.reason

    @pytest.mark.parametrize("order", [("u", "a"), ("a", "u")])
    def test_an_unknown_length_alone_with_one_recording_is_the_same(self, order):
        """Taken without asking, and the known-length entry is the one added."""
        results = [dict(result("Song Name", "Some Artist", vid=v),
                        **({"duration": "3:00"} if v == "a" else {})) for v in order]
        d = decide("Some Artist", "Song Name", *results)
        assert d.status == "high" and d.video_id == "a"

    def test_every_version_is_listed(self):
        """More versions than the usual three alternatives: all of them shown."""
        titles = ["Song Name"] + [f"Song Name ({x})" for x in
                                  ("Live", "Deluxe", "From X", "Radio Edit", "Demo")]
        d = decide("Some Artist", "Song Name",
                   *[result(t, "Some Artist", vid=t) for t in titles])
        assert {c.video_id for c in d.choices} == set(titles)

    def test_another_artists_version_is_not_a_second_version(self):
        """A cover by someone else is a rival, not a version of *this* song."""
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall (Live)", "Oasis", vid="a"),
                   result("Wonderwall", "Tribute Players", vid="b"))
        assert d.status == "high" and d.video_id == "a"

    def test_brackets_on_the_request_are_ignored_too(self):
        d = decide("Some Artist", "Song Name (Pt. 2)", result("Song Name (Pt. 1)", "Some Artist"))
        assert d.status == "high", d


class TestUserTextKeepsItsDashes:
    """" - …" is YouTube Music's metadata convention, not the user's: a folder
    import's title is the whole filename."""

    def test_a_folder_filename_still_finds_the_song(self):
        d = decide("Radio Nova", "Phoenix - Lisztomania", result("Lisztomania", "Phoenix"))
        assert d.status in OFFERED and d.video_id == "v1"

    @pytest.mark.parametrize("artist, title, got", [
        ("Queen", "Bohemian Rhapsody - Remastered 2011", "Bohemian Rhapsody"),
        ("The Beatles", "Here Comes the Sun - Remastered 2009", "Here Comes The Sun"),
        ("Oasis", "Wonderwall - Live", "Wonderwall (Live at Wembley)"),
    ])
    def test_a_dash_suffix_in_a_spotify_export_is_metadata(self, artist, title, got):
        """Exportify writes " - Remastered 2011": that reading must win too."""
        d = decide(artist, title, result(got, artist))
        assert d.status == "high", d

    def test_a_dash_on_the_result_is_still_metadata(self):
        d = decide("Oasis", "Wonderwall", result("Wonderwall - Remastered 2011", "Oasis"))
        assert d.status == "high"


class TestMatchKey:
    """Same artist and title once spaces and any bracketed text are ignored."""

    @pytest.mark.parametrize("artist, title, got_artist, got_title", [
        ("Artist (UK)", "Song", "Artist", "Song"),
        ("Artist", "Song", "Artist [FR]", "Song"),
        ("Artist Name", "Song", "ArtistName (Official)", "Song"),
        ("Artist", "(I Can't Get No) Satisfaction", "Artist", "Satisfaction"),
        ("Artist", "Song [Live] (2004 Remaster)", "Artist", "Song"),
        ("Artist", "Song", "Artist", "Song {Bonus} <Edit>"),
        ("Artist", "Song (feat. X (Y))", "Artist", "Song"),
        ("アーティスト", "歌", "アーティスト", "歌【Live】"),
        ("Air & Phoenix", "Song", "Air, Phoenix", "Song"),
    ])
    def test_a_lone_match_is_high(self, artist, title, got_artist, got_title):
        d = decide(artist, title, result(got_title, *got_artist.split(", ")))
        assert d.status == "high", d

    @pytest.mark.parametrize("artist, title, got_artist, got_title", [
        ("Artist", "Song", "Other Band", "Song"),
        ("Artist", "Song", "Artist", "Songs"),
        ("Artist", "One", "Artist", "Someone"),
    ])
    def test_a_real_difference_is_not_a_match(self, artist, title, got_artist, got_title):
        d = decide(artist, title, result(got_title, got_artist))
        assert d.status != "high", d

    def test_a_title_that_is_all_metadata_is_kept_whole(self):
        assert strip_metadata("(Intro)") == "(Intro)"
        d = decide("Artist", "(Intro)", result("(Intro)", "Artist"))
        assert d.status == "high"

    def test_nested_brackets_peel_completely(self):
        assert strip_metadata("Song (Live (Remastered) 2004)").strip() == "Song"

    def test_several_versions_ask(self):
        d = decide("Artist", "Song",
                   result("Song [Live]", "Artist (UK)", vid="a"),
                   result("Song", "Artist", vid="b"))
        assert d.status == "ambiguous"
        assert {c.video_id for c in d.choices} == {"a", "b"}


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
        assert d.candidate.principal_score < HIGH_ARTIST
        assert "Alicia Keys" in d.reason

    def test_a_guest_cannot_lift_a_wrong_principal_over_the_bar(self):
        d = decide("Jay-Z feat. Alicia Keys", "Empire State of Mind",
                   result("Empire State of Mind", "Alicia Keys", "Someone Else"))
        assert d.candidate.principal_score < HIGH_ARTIST

    def test_with_is_a_separator_in_artists(self):
        """Unlike titles, artist credits really do use "with"."""
        d = decide("Ella Fitzgerald with Louis Armstrong", "Cheek to Cheek",
                   result("Cheek to Cheek", "Ella Fitzgerald", "Louis Armstrong"))
        assert d.status == "high"

    def test_a_guest_bonus_cannot_carry_a_near_miss_principal_over_the_bar(self):
        """The bonus is for ranking only. Comparing the *combined* score against
        HIGH_ARTIST let one matching guest lift a 0.833 principal to 0.883 and
        clear the 0.88 gate the principal itself had failed."""
        want = "Nick Cave and the Bad Seeds feat. Guest Star"
        got = "Nick Cave & The Bad Seeds"          # 0.833 — just under the 0.88 gate
        principal, combined = score_artist(want, [{"name": got}, {"name": "Guest Star"}])
        assert principal < HIGH_ARTIST <= combined, "the reachable window this guards"
        d = decide(want, "A Sufficiently Long Title",
                   result("A Sufficiently Long Title", got, "Guest Star"))
        assert d.status != "high"
        assert "artist similarity" in d.reason

    def test_the_guest_bonus_still_helps_a_candidate_win(self):
        """Ranking must still prefer the result crediting everyone asked for —
        the bonus was removed from the *gate*, not from the score."""
        want, got = "Calvin Harris feat. Rihanna", "Calvin Harris Music"
        assert (score_artist(want, [{"name": got}, {"name": "Rihanna"}])[1]
                > score_artist(want, [{"name": got}])[1])
        d = decide(want, "This Is What You Came For",
                   result("This Is What You Came For", got, vid="solo"),
                   result("This Is What You Came For", got, "Rihanna", vid="both"))
        assert d.video_id == "both"


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
                   result("Wonderwall (From the Film X)", "Oasis", vid="a"),
                   result("Wonderwall (Pt. 2)", "Oasis", vid="b"))
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
        assert d.candidate.title_score >= 0.80

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
        ("One More Time", "One More Time (Skrillex Remix)", "remix version"),
        ("Stronger", "Stronger (Instrumental)", "instrumental version"),
        ("Wonderwall", "Wonderwall (Cover)", "cover version"),
        ("Bad Habit", "Bad Habit (Sped Up)", "sped up version"),
        ("Stan", "Stan (Explicit)", "explicit version"),
    ])
    def test_a_different_recording_is_offered_not_refused(self, title, got, expected_in_reason):
        """Alone it is simply taken (see TestUnrequestedSuffix); next to the
        plain recording it is offered as a choice, with the difference named."""
        d = decide("Oasis", title, result(got, "Oasis", vid="v1"),
                   result(title, "Oasis", vid="plain"))
        assert d.status in OFFERED
        assert "v1" in {c.video_id for c in d.choices}, "the user should get the chance to take it"
        assert expected_in_reason in [c for c in d.choices if c.video_id == "v1"][0].reason

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
    """Asking for a specific recording when the result's brackets say otherwise.

    Brackets are ignored on both sides when deciding whether a result *is* the
    requested song, so a lone result is taken whatever version it is — but the
    difference is still named on the candidate, and when several versions exist
    the one actually asked for is proposed first.
    """

    @pytest.mark.parametrize("asked, marker", [
        ("Wonderwall (Live)", "live"),
        ("Wonderwall (Acoustic)", "acoustic"),
        ("Wonderwall (Remix)", "remix"),
        ("Wonderwall (Instrumental)", "instrumental"),
    ])
    def test_the_only_recording_is_taken(self, asked, marker):
        d = decide("Oasis", asked, result("Wonderwall", "Oasis"))
        assert d.status == "high"
        assert d.video_id == "v1"
        assert f"no {marker} version found" in d.candidate.reason

    def test_the_requested_version_is_proposed_first(self):
        """Two versions are a choice, but the one asked for leads, whatever
        YouTube Music's order."""
        for order in ([("Wonderwall", "album"), ("Wonderwall (Live)", "live")],
                      [("Wonderwall (Live)", "live"), ("Wonderwall", "album")]):
            d = decide("Oasis", "Wonderwall (Live)",
                       *[result(t, "Oasis", vid=v) for t, v in order])
            assert d.status == "ambiguous", d
            assert d.video_id == "live"
            assert [a.video_id for a in d.alternatives] == ["album"]

    @pytest.mark.parametrize("asked, offered, marker", [
        ("Wonderwall (Live)", "Wonderwall (Remix)", "remix"),
        ("Wonderwall (Acoustic)", "Wonderwall (Karaoke)", "karaoke"),
        ("Wonderwall (Live Acoustic)", "Wonderwall (Live)", "no acoustic version"),
    ])
    def test_a_differently_marked_lone_version_is_taken(self, asked, offered, marker):
        d = decide("Oasis", asked, result(offered, "Oasis"))
        assert d.status == "high"
        assert marker in d.candidate.reason, "the difference is still recorded"

    def test_the_standard_recording_outranks_a_wrongly_marked_one(self):
        """Both are imperfect, but being handed the plain recording is a milder
        disappointment than being handed a remix nobody asked for."""
        d = decide("Oasis", "Wonderwall (Live)",
                   result("Wonderwall (Remix)", "Oasis", vid="remix"),
                   result("Wonderwall", "Oasis", vid="standard"))
        assert d.video_id == "standard"

    def test_the_mismatch_names_the_unwanted_marker(self):
        d = decide("Oasis", "Wonderwall", result("Wonderwall (Karaoke)", "Oasis"))
        assert "this is the karaoke version, not the one asked for" in d.candidate.reason


class TestAmbiguity:
    def test_a_clear_winner_stays_high(self):
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall", "Oasis", vid="a"),
                   result("Champagne Supernova", "Oasis", vid="b"))
        assert d.status == "high" and d.video_id == "a"

    def test_without_a_sure_artist_it_is_offered_not_taken(self):
        """When the artist isn't a sure match these aren't versions of the
        requested song: the best is offered, with the shortfall named."""
        d = decide("Nick Cave & The Bad Seeds", "Red Right Hand",
                   result("Red Right Hand", "Nick Cave and the Bad Seeds", vid="a"),
                   result("Red Right Hand (Live)", "Nick Cave and the Bad Seeds", vid="b"))
        assert d.status == "ambiguous"
        assert d.video_id == "a"
        assert "artist similarity" in d.reason

    def test_the_plain_title_is_proposed_first_among_versions(self):
        """On a tie, the literal title leads, whatever YouTube Music's order."""
        for order in ([("Wonderwall", "a"), ("Wonderwall (Deluxe)", "b")],
                      [("Wonderwall (Deluxe)", "b"), ("Wonderwall", "a")]):
            d = decide("Oasis", "Wonderwall",
                       *[result(t, "Oasis", vid=v) for t, v in order])
            assert d.video_id == "a", order

    def test_the_version_penalty_ranks_the_studio_cut_first(self):
        """A live result alongside the studio one is still a candidate — nothing
        is filtered — but VERSION_PENALTY ranks the studio version first."""
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall (Live)", "Oasis", vid="a"),
                   result("Wonderwall", "Oasis", vid="b"))
        assert d.video_id == "b"
        assert d.alternatives[0].video_id == "a", "the live take is ranked, not discarded"

    def test_alternatives_are_offered_for_review(self):
        d = decide("Oasis", "Wonderwall",
                   result("Wonderwall (From the Film X)", "Oasis", vid="a"),
                   result("Wonderwall (Pt. 2)", "Oasis", vid="b"))
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
