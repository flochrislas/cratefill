"""Tests for the YouTube Music worker functions, against a fake client.

No Tk window, no network, no browser.json. The functions under test take a `put`
callable, so a plain list stands in for the UI's worker queue.
"""

import contextlib
import queue

import pytest

from cratefill import youtube
from cratefill.app import CratefillApp


class FakeYT:
    """Stands in for a YTMusic client, recording what was asked of it."""

    def __init__(self, search_results=None, add_status="STATUS_SUCCEEDED",
                 existing_tracks=(), fail_on=()):
        self.search_results = search_results
        self.add_status = add_status
        self.existing_tracks = list(existing_tracks)
        self.fail_on = set(fail_on)
        self.calls = []
        self.added = []

    def _maybe_fail(self, what):
        if what in self.fail_on:
            raise RuntimeError(f"{what} exploded")

    def search(self, query, **kwargs):
        self.calls.append(("search", query))
        self._maybe_fail("search")
        if self.search_results is None:
            # Echo the query back as a perfect match, so the default fake always
            # produces a high-confidence decision.
            artist, _, title = query.partition(" ")
            return [{"videoId": "v-" + query.replace(" ", "-"),
                     "title": title,
                     "artists": [{"name": artist}]}]
        return self.search_results

    def add_playlist_items(self, playlist_id, video_ids, **kwargs):
        self.calls.append(("add", playlist_id, tuple(video_ids)))
        self._maybe_fail("add")
        self.added.append(list(video_ids))
        status = self.add_status(len(self.added)) if callable(self.add_status) else self.add_status
        return {"status": status}

    def get_playlist(self, playlist_id, **kwargs):
        self.calls.append(("get_playlist", playlist_id))
        self._maybe_fail("get_playlist")
        return {"tracks": self.existing_tracks}

    def get_library_playlists(self, **kwargs):
        self.calls.append(("get_library_playlists",))
        self._maybe_fail("get_library_playlists")
        return [{"playlistId": "PL1", "title": "Road trip", "count": 42}]


def kinds(messages, kind):
    return [payload for k, payload in messages if k == kind]


def called(yt, what):
    return [c for c in yt.calls if c[0] == what]


PLAYLIST = {"playlistId": "PL1", "title": "Road trip"}
SONG = ("Phoenix", "Lisztomania", "Radio Nova")


class TestFetchPlaylists:
    def test_hands_the_library_to_the_ui(self):
        out = []
        youtube.fetch_playlists(FakeYT(), out.append)
        assert kinds(out, "playlists") == [
            [{"playlistId": "PL1", "title": "Road trip", "count": 42}]
        ]

    def test_failure_is_reported_not_raised(self):
        """Callers use this as a final step; it must not lose their earlier output."""
        out = []
        youtube.fetch_playlists(FakeYT(fail_on=["get_library_playlists"]), out.append)
        assert kinds(out, "playlists") == []
        assert "Could not fetch playlists" in kinds(out, "log")[0]
        assert kinds(out, "account") == ["Login expired? Re-log in."]


class TestEvaluateSongs:
    def test_never_touches_a_playlist(self):
        """The whole reason matching is a separate phase: the user can still
        cancel, so nothing may be mutated while decisions are outstanding."""
        yt = FakeYT()
        youtube.evaluate_songs(yt, [SONG], [].append)
        assert called(yt, "add") == []
        assert called(yt, "search") != []

    def test_returns_a_decision_per_song(self):
        evaluated = youtube.evaluate_songs(FakeYT(), [SONG, SONG], [].append)
        assert len(evaluated) == 2
        assert all(d.status == "high" for _song, d in evaluated)

    def test_station_is_not_part_of_the_query(self):
        """The station column is context for the user, never a search term."""
        yt = FakeYT()
        youtube.evaluate_songs(yt, [SONG], [].append)
        assert ("search", "Phoenix Lisztomania") in yt.calls

    def test_emits_one_step_per_song(self):
        out = []
        youtube.evaluate_songs(FakeYT(), [SONG, SONG, SONG], out.append)
        assert len(kinds(out, "step")) == 3

    def test_search_failure_becomes_a_rejection(self):
        out = []
        evaluated = youtube.evaluate_songs(FakeYT(fail_on=["search"]), [SONG], out.append)
        assert evaluated[0][1].status == "rejected"
        assert "search failed" in evaluated[0][1].reason
        assert any("no credible match" in line for line in kinds(out, "log"))

    def test_an_incomplete_row_is_rejected_without_searching(self):
        """Don't spend a network call on a row that can never match."""
        yt = FakeYT()
        evaluated = youtube.evaluate_songs(yt, [("", "", "")], [].append)
        assert evaluated[0][1].status == "rejected"
        assert called(yt, "search") == []

    def test_logs_an_unrelated_result_as_no_match(self):
        out = []
        results = [{"videoId": "v1", "title": "Something Else", "artists": [{"name": "Nobody"}]}]
        youtube.evaluate_songs(FakeYT(search_results=results), [SONG], out.append)
        assert kinds(out, "log")[0].startswith("✗ Phoenix — Lisztomania")

    def test_logs_a_wrong_artist_as_uncertain_rather_than_no_match(self):
        """The right song by the wrong artist is offered, not discarded."""
        out = []
        results = [{"videoId": "v1", "title": "Lisztomania", "artists": [{"name": "Nobody"}]}]
        evaluated = youtube.evaluate_songs(FakeYT(search_results=results), [SONG], out.append)
        assert evaluated[0][1].status == "ambiguous"
        assert kinds(out, "log")[0].startswith("? Phoenix — Lisztomania")

    def test_ambiguous_log_shows_the_proposal(self):
        out = []
        results = [
            {"videoId": "a", "title": "Lisztomania", "artists": [{"name": "Phoenix"}]},
            {"videoId": "b", "title": "Lisztomania (Deluxe)", "artists": [{"name": "Phoenix"}]},
        ]
        evaluated = youtube.evaluate_songs(FakeYT(search_results=results), [SONG], out.append)
        assert evaluated[0][1].status == "ambiguous"
        line = kinds(out, "log")[0]
        assert line.startswith("? Phoenix — Lisztomania")
        assert "Proposed: Phoenix — Lisztomania" in line


class TestAddVideoIdsToPlaylists:
    def test_happy_path(self):
        out, yt = [], FakeYT()
        youtube.add_video_ids_to_playlists(yt, ["v1"], [PLAYLIST], out.append)
        assert yt.added == [["v1"]]
        assert "--- Done. ---" in kinds(out, "log")
        assert any("Added 1 song(s) to 'Road trip'" in line for line in kinds(out, "log"))

    def test_duplicate_video_ids_are_collapsed(self):
        """Two CSV rows can resolve to the same YT song."""
        yt = FakeYT()
        youtube.add_video_ids_to_playlists(yt, ["same", "same"], [PLAYLIST], [].append)
        assert yt.added == [["same"]]

    def test_retries_without_the_songs_already_in_the_playlist(self):
        """YT Music fails the whole batch if one song is already there, so the
        second attempt must drop the ones the playlist already contains."""
        out = []
        yt = FakeYT(
            add_status=lambda attempt: "STATUS_FAILED" if attempt == 1 else "STATUS_SUCCEEDED",
            existing_tracks=[{"videoId": "old"}],
        )
        youtube.add_video_ids_to_playlists(yt, ["new", "old"], [PLAYLIST], out.append)
        assert yt.added[0] == ["new", "old"]   # first attempt, fails atomically
        assert yt.added[1] == ["new"]          # retry without the duplicate
        assert any("1 already there, skipped" in line for line in kinds(out, "log"))

    def test_reports_when_everything_was_already_present(self):
        out = []
        yt = FakeYT(add_status="STATUS_FAILED", existing_tracks=[{"videoId": "old"}])
        youtube.add_video_ids_to_playlists(yt, ["old"], [PLAYLIST], out.append)
        assert any("already in the playlist" in line for line in kinds(out, "log"))
        assert len(yt.added) == 1  # no pointless second attempt

    def test_one_failing_playlist_does_not_stop_the_others(self):
        out = []
        two = [PLAYLIST, {"playlistId": "PL2", "title": "Chill"}]
        youtube.add_video_ids_to_playlists(FakeYT(fail_on=["add"]), ["v1"], two, out.append)
        assert sum("Failed to add" in line for line in kinds(out, "log")) == 2

    def test_nothing_to_add(self):
        yt = FakeYT()
        youtube.add_video_ids_to_playlists(yt, [], [PLAYLIST], [].append)
        assert called(yt, "add") == []

    def test_emits_one_step_per_playlist(self):
        out = []
        two = [PLAYLIST, {"playlistId": "PL2", "title": "Chill"}]
        youtube.add_video_ids_to_playlists(FakeYT(), ["v1"], two, out.append)
        assert len(kinds(out, "step")) == 2


class Stub:
    """Just enough of CratefillApp for its worker wrappers, without a Tk window."""

    def __init__(self):
        self.worker_queue = queue.Queue()

    def drain(self):
        out = []
        while not self.worker_queue.empty():
            out.append(self.worker_queue.get_nowait())
        return out


class TestCompletionGuarantee:
    """The Add/Export buttons only come back on ("done", …); a worker that dies
    without emitting it leaves the UI disabled until restart."""

    @pytest.mark.parametrize("worker, args", [
        ("_worker", ("songs", "playlists")),
        ("_add_worker", (["v1"], "playlists")),
        ("_export_worker", ("playlists", "dest")),
    ])
    def test_done_is_emitted_even_when_the_work_explodes(self, worker, args, monkeypatch):
        def boom(*a, **k):
            raise TypeError("'NoneType' object is not iterable")

        for name in ("evaluate_songs", "add_video_ids_to_playlists",
                     "export_playlists_to_csv", "fetch_playlists"):
            monkeypatch.setattr(youtube, name, boom)
        stub = Stub()
        # The thread may still die noisily — what must not happen is the UI
        # never hearing about it. Tolerate the exception, then check the queue.
        with contextlib.suppress(Exception):
            getattr(CratefillApp, worker)(stub, FakeYT(), *args)
        messages = stub.drain()
        assert ("done", None) in messages, "UI would stay disabled until restart"
        assert messages[-1] == ("done", None), "done must be the last word"

    def test_unexpected_failures_are_logged_for_the_user(self, monkeypatch):
        def boom(*a, **k):
            raise TypeError("'NoneType' object is not iterable")

        monkeypatch.setattr(youtube, "evaluate_songs", boom)
        stub = Stub()
        CratefillApp._worker(stub, FakeYT(), [SONG], [PLAYLIST])
        logs = kinds(stub.drain(), "log")
        assert any("Unexpected error while matching: TypeError" in line for line in logs)

    def test_matching_phase_hands_its_decisions_over_for_review(self):
        stub, yt = Stub(), FakeYT()
        CratefillApp._worker(stub, yt, [SONG], [PLAYLIST])
        messages = stub.drain()
        handovers = kinds(messages, "decisions")
        assert len(handovers) == 1
        client, evaluated, playlists = handovers[0]
        assert client is yt and playlists == [PLAYLIST]
        assert [d.status for _song, d in evaluated] == ["high"]
        assert messages[-1] == ("done", None)
        assert called(yt, "add") == [], "phase one must not mutate anything"

    def test_a_failed_matching_phase_hands_over_nothing(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("nope")

        monkeypatch.setattr(youtube, "evaluate_songs", boom)
        stub = Stub()
        CratefillApp._worker(stub, FakeYT(), [SONG], [PLAYLIST])
        assert kinds(stub.drain(), "decisions") == []

    def test_add_phase_refetches_playlists_so_counts_stay_current(self):
        stub, yt = Stub(), FakeYT()
        CratefillApp._add_worker(stub, yt, ["v1"], [PLAYLIST])
        messages = stub.drain()
        assert kinds(messages, "playlists")       # refetched on the worker thread
        assert messages[-1] == ("done", None)
