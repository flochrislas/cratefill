"""Tests for the review step between matching and adding. No Tk window.

`CratefillApp._review_and_add` is called as an unbound method on a stub, which
keeps these tests free of a Tk instance while still exercising the real code that
decides what reaches a playlist.
"""

import pytest

from cratefill import app as app_module
from cratefill import policy
from cratefill.app import CratefillApp
from cratefill.matching import Candidate, MatchDecision

PLAYLIST = {"playlistId": "PL1", "title": "Road trip"}


def candidate(vid, title="T", artist="A", score=0.9):
    return Candidate({"videoId": vid, "title": title, "artists": [{"name": artist}]},
                     title_score=score, artist_score=score, overall_score=score,
                     relation="same")


def high(vid="v-high"):
    return MatchDecision("high", candidate=candidate(vid))


def ambiguous(vid="v-amb", alternatives=()):
    return MatchDecision("ambiguous", candidate=candidate(vid),
                         reasons=["another candidate scores almost the same"],
                         alternatives=list(alternatives))


def weak(vid="v-weak"):
    return MatchDecision("weak", candidate=candidate(vid, score=0.4),
                         reasons=["title similarity 0.33 below 0.90"])


def rejected():
    return MatchDecision("rejected", reasons=["no result with a related title"])


class FakeThread:
    """Runs nothing — the test only cares which video ids were handed over."""

    started = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        FakeThread.started.append(self.args)


class ReviewStub:
    """Enough of CratefillApp to run _review_and_add and _ask_about_match."""

    def __init__(self, saved_policy="ask", answers=None):
        self.ambiguous_policy = saved_policy
        self.answers = list(answers or [])      # queued (action, remember) replies
        self.asked = []                         # (artist, title) per prompt shown
        self.logs = []
        self.saved = []                         # policies written through the UI
        self.work_started = []

    # --- the bits _review_and_add leans on ---
    def log(self, message):
        self.logs.append(message)

    def _start_work(self, maximum=None):
        self.work_started.append(maximum)

    def set_ambiguous_policy(self, value):
        self.ambiguous_policy = value
        self.saved.append(value)

    def _ask_about_match(self, artist, title, decision):
        """Stands in for the modal: pops the next queued answer.

        Answers may be (action, remember) or (action, remember, chosen_index) to
        pick one of the alternatives, mirroring the dialog's radio list.
        """
        self.asked.append((artist, title))
        answer = self.answers.pop(0)
        action, remember = answer[0], answer[1]
        chosen = decision.choices[answer[2]] if len(answer) > 2 else decision.candidate
        if remember and action:
            self.set_ambiguous_policy(action)
        return action, chosen

    def _add_worker(self, *args):
        raise AssertionError("the add worker must only run via a thread")


@pytest.fixture(autouse=True)
def no_threads(monkeypatch):
    FakeThread.started = []
    fake = type("FakeThreading", (), {"Thread": FakeThread})
    monkeypatch.setattr(app_module, "threading", fake)
    return FakeThread


def review(stub, decisions, playlists=(PLAYLIST,)):
    evaluated = [(("A", f"T{i}", ""), d) for i, d in enumerate(decisions)]
    CratefillApp._review_and_add(stub, "yt", evaluated, list(playlists))
    return FakeThread.started


def approved_ids(started):
    """The video ids handed to the add worker, or None if it never started."""
    if not started:
        return None
    _yt, video_ids, _playlists = started[0]
    return video_ids


class TestHighConfidence:
    @pytest.mark.parametrize("saved", ["ask", "skip", "add"])
    def test_added_without_a_prompt_whatever_the_policy(self, saved):
        stub = ReviewStub(saved)
        started = review(stub, [high()])
        assert approved_ids(started) == ["v-high"]
        assert stub.asked == [], "a certain match must not interrupt the user"


class TestRejected:
    @pytest.mark.parametrize("saved", ["ask", "skip", "add"])
    def test_never_added_and_never_prompted(self, saved):
        """"Always add" must not rescue a match that failed a threshold."""
        stub = ReviewStub(saved)
        started = review(stub, [rejected()])
        assert started == [], "nothing was approved, so no add job should run"
        assert stub.asked == []


class TestPolicyWithoutPrompting:
    def test_always_skip_drops_ambiguous_matches(self):
        stub = ReviewStub("skip")
        started = review(stub, [ambiguous()])
        assert started == []
        assert stub.asked == []
        assert any("skipped by policy" in line for line in stub.logs)

    def test_always_add_accepts_ambiguous_matches(self):
        stub = ReviewStub("add")
        started = review(stub, [ambiguous()])
        assert approved_ids(started) == ["v-amb"]
        assert stub.asked == []
        assert any("added by policy" in line for line in stub.logs)


class TestAlwaysAsk:
    def test_add_approves_only_that_song(self):
        stub = ReviewStub("ask", answers=[(policy.ADD, False)])
        started = review(stub, [ambiguous()])
        assert approved_ids(started) == ["v-amb"]
        assert stub.asked == [("A", "T0")]
        assert stub.saved == [], "an unremembered choice must not change the policy"

    def test_skip_leaves_it_out(self):
        stub = ReviewStub("ask", answers=[(policy.SKIP, False)])
        assert review(stub, [ambiguous()]) == []
        assert stub.saved == []

    def test_each_ambiguous_song_is_asked_about(self):
        stub = ReviewStub("ask", answers=[(policy.ADD, False), (policy.SKIP, False)])
        started = review(stub, [ambiguous("a"), ambiguous("b")])
        assert approved_ids(started) == ["a"]
        assert len(stub.asked) == 2

    def test_remembering_add_applies_to_the_rest_of_the_run(self):
        """One answer, then no more questions — and the saved policy changed."""
        stub = ReviewStub("ask", answers=[(policy.ADD, True)])
        started = review(stub, [ambiguous("a"), ambiguous("b"), ambiguous("c")])
        assert approved_ids(started) == ["a", "b", "c"]
        assert len(stub.asked) == 1, "later songs must follow the remembered choice"
        assert stub.saved == ["add"]
        assert stub.ambiguous_policy == "add"

    def test_remembering_skip_applies_to_the_rest_of_the_run(self):
        stub = ReviewStub("ask", answers=[(policy.SKIP, True)])
        started = review(stub, [ambiguous("a"), ambiguous("b")])
        assert started == []
        assert len(stub.asked) == 1
        assert stub.ambiguous_policy == "skip"


class TestWeakMatches:
    """A weak match is offered but never automated, whatever the policy says."""

    @pytest.mark.parametrize("saved", ["ask", "skip", "add"])
    def test_always_prompts(self, saved):
        stub = ReviewStub(saved, answers=[(policy.ADD, False)])
        started = review(stub, [weak()])
        assert stub.asked == [("A", "T0")], f"policy {saved} must not decide this"
        assert approved_ids(started) == ["v-weak"]

    def test_always_add_does_not_silently_take_it(self):
        """The case the review flagged: one shared word authorising a song."""
        stub = ReviewStub("add", answers=[(policy.SKIP, False)])
        assert review(stub, [weak()]) == []
        assert len(stub.asked) == 1

    def test_a_remembered_add_still_does_not_skip_weak_prompts(self):
        stub = ReviewStub("ask", answers=[(policy.ADD, True), (policy.ADD, False)])
        review(stub, [ambiguous("amb"), weak("wk")])
        assert stub.ambiguous_policy == "add"
        assert len(stub.asked) == 2, "the weak one must still be asked about"


class TestChoosingAnAlternative:
    def test_the_picked_candidate_is_what_gets_added(self):
        """The dialog lists near-scoring rivals; whichever is picked is the one
        that must reach the playlist."""
        alt = candidate("v-alt", title="Other")
        stub = ReviewStub("ask", answers=[(policy.ADD, False, 1)])
        started = review(stub, [ambiguous("v-winner", alternatives=[alt])])
        assert approved_ids(started) == ["v-alt"]

    def test_the_winner_is_used_when_nothing_is_picked(self):
        alt = candidate("v-alt", title="Other")
        stub = ReviewStub("ask", answers=[(policy.ADD, False)])
        started = review(stub, [ambiguous("v-winner", alternatives=[alt])])
        assert approved_ids(started) == ["v-winner"]


class TestCancelling:
    def test_dismissing_the_prompt_mutates_nothing(self):
        """Not even the high-confidence songs already approved: a cancelled
        review must leave every playlist untouched."""
        stub = ReviewStub("ask", answers=[(None, False)])
        started = review(stub, [high(), ambiguous(), high("v-later")])
        assert started == [], "no add job may start after a cancel"
        assert any("Cancelled" in line for line in stub.logs)

    def test_stops_asking_after_a_cancel(self):
        stub = ReviewStub("ask", answers=[(None, False)])
        review(stub, [ambiguous("a"), ambiguous("b")])
        assert len(stub.asked) == 1


class TestReporting:
    def test_counts_are_summarised(self):
        stub = ReviewStub("skip")
        review(stub, [high(), ambiguous(), rejected()])
        assert any("1 to add, 2 skipped" in line for line in stub.logs)

    def test_says_so_when_nothing_is_approved(self):
        stub = ReviewStub("skip")
        review(stub, [rejected()])
        assert any("no playlist was changed" in line for line in stub.logs)

    def test_progress_is_sized_by_playlists_for_the_add_phase(self):
        stub = ReviewStub("add")
        two = [PLAYLIST, {"playlistId": "PL2", "title": "Chill"}]
        review(stub, [ambiguous()], playlists=two)
        assert stub.work_started == [2]
