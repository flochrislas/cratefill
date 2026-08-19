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
            return [{"videoId": "v-" + query.replace(" ", "-"),
                     "title": query.split(" ", 1)[-1],
                     "artists": [{"name": query.split(" ", 1)[0]}]}]
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


PLAYLIST = {"playlistId": "PL1", "title": "Road trip"}
SONG = ("Phoenix", "Lisztomania", "Radio Nova")


class TestFetchPlaylists:
    def test_hands_the_library_to_the_ui(self):
        out = []
        youtube.fetch_playlists(FakeYT(), out.append)
        assert kinds(out, "playlists") == [[{"playlistId": "PL1", "title": "Road trip", "count": 42}]]

    def test_failure_is_reported_not_raised(self):
        """Callers use this as a final step; it must not lose their earlier output."""
        out = []
        youtube.fetch_playlists(FakeYT(fail_on=["get_library_playlists"]), out.append)
        assert kinds(out, "playlists") == []
        assert "Could not fetch playlists" in kinds(out, "log")[0]
        assert kinds(out, "account") == ["Login expired? Re-log in."]


class TestAddSongsToPlaylists:
    def test_happy_path(self):
        out, yt = [], FakeYT()
        youtube.add_songs_to_playlists(yt, [SONG], [PLAYLIST], out.append)
        assert yt.added == [["v-Phoenix-Lisztomania"]]
        assert "✓ Phoenix — Lisztomania" in kinds(out, "log")
        assert "--- Done. 1 matched, 0 not found. ---" in kinds(out, "log")

    def test_station_is_not_part_of_the_query(self):
        """The station column is context for the user, never a search term."""
        yt = FakeYT()
        youtube.add_songs_to_playlists(yt, [SONG], [PLAYLIST], [].append)
        assert ("search", "Phoenix Lisztomania") in yt.calls

    def test_search_failure_is_counted_not_fatal(self):
        out, yt = [], FakeYT(fail_on=["search"])
        youtube.add_songs_to_playlists(yt, [SONG], [PLAYLIST], out.append)
        logs = kinds(out, "log")
        assert any("search failed" in line for line in logs)
        assert "--- Done. 0 matched, 1 not found. ---" in logs
        assert yt.added == []

    def test_no_match_is_reported(self):
        out, yt = [], FakeYT(search_results=[])
        youtube.add_songs_to_playlists(yt, [SONG], [PLAYLIST], out.append)
        assert any("no match found" in line for line in kinds(out, "log"))
        assert yt.added == []

    def test_uncertain_match_is_flagged_but_still_added(self):
        out = []
        yt = FakeYT(search_results=[
            {"videoId": "v9", "title": "Something Else", "artists": [{"name": "Nobody"}]}
        ])
        youtube.add_songs_to_playlists(yt, [SONG], [PLAYLIST], out.append)
        assert any(line.startswith("? Phoenix — Lisztomania") for line in kinds(out, "log"))
        assert yt.added == [["v9"]]

    def test_duplicate_video_ids_are_collapsed(self):
        """Two CSV rows can resolve to the same YT song."""
        out = []
        yt = FakeYT(search_results=[
            {"videoId": "same", "title": "Lisztomania", "artists": [{"name": "Phoenix"}]}
        ])
        youtube.add_songs_to_playlists(yt, [SONG, SONG], [PLAYLIST], out.append)
        assert yt.added == [["same"]]

    def test_retries_without_the_songs_already_in_the_playlist(self):
        """YT Music fails the whole batch if one song is already there, so the
        second attempt must drop the ones the playlist already contains."""
        out = []
        yt = FakeYT(
            search_results=None,
            add_status=lambda attempt: "STATUS_FAILED" if attempt == 1 else "STATUS_SUCCEEDED",
            existing_tracks=[{"videoId": "v-Air-Sexy-Boy"}],
        )
        songs = [("Phoenix", "Lisztomania", ""), ("Air", "Sexy Boy", "")]
        youtube.add_songs_to_playlists(yt, songs, [PLAYLIST], out.append)
        assert yt.added[0] == ["v-Phoenix-Lisztomania", "v-Air-Sexy-Boy"]  # first, atomic failure
        assert yt.added[1] == ["v-Phoenix-Lisztomania"]                    # retry without the dupe
        assert any("1 already there, skipped" in line for line in kinds(out, "log"))

    def test_reports_when_everything_was_already_present(self):
        out = []
        yt = FakeYT(
            search_results=None,
            add_status="STATUS_FAILED",
            existing_tracks=[{"videoId": "v-Phoenix-Lisztomania"}],
        )
        youtube.add_songs_to_playlists(yt, [SONG], [PLAYLIST], out.append)
        assert any("already in the playlist" in line for line in kinds(out, "log"))
        assert len(yt.added) == 1  # no pointless second attempt

    def test_one_failing_playlist_does_not_stop_the_others(self):
        out = []
        yt = FakeYT(fail_on=["add"])
        two = [PLAYLIST, {"playlistId": "PL2", "title": "Chill"}]
        youtube.add_songs_to_playlists(yt, [SONG], two, out.append)
        logs = kinds(out, "log")
        assert sum("Failed to add" in line for line in logs) == 2

    def test_emits_one_step_per_song_and_per_playlist(self):
        out = []
        youtube.add_songs_to_playlists(FakeYT(), [SONG, SONG], [PLAYLIST], out.append)
        assert len(kinds(out, "step")) == 3  # 2 songs + 1 playlist


class TestExportPlaylistsToCsv:
    def test_writes_one_csv_per_playlist(self, tmp_path):
        out = []
        yt = FakeYT(existing_tracks=[
            {"title": "Lisztomania", "artists": [{"name": "Phoenix"}], "album": {"name": "W"}}
        ])
        youtube.export_playlists_to_csv(yt, [PLAYLIST], tmp_path, out.append)
        assert (tmp_path / "Road trip.csv").exists()
        assert any("Saved 'Road trip' (1 tracks)" in line for line in kinds(out, "log"))
        assert "--- Export done. ---" in kinds(out, "log")

    def test_failure_on_one_playlist_is_reported(self, tmp_path):
        out = []
        youtube.export_playlists_to_csv(
            FakeYT(fail_on=["get_playlist"]), [PLAYLIST], tmp_path, out.append
        )
        assert any("Failed to export 'Road trip'" in line for line in kinds(out, "log"))
        assert "--- Export done. ---" in kinds(out, "log")


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
        ("_export_worker", ("playlists", "dest")),
    ])
    def test_done_is_emitted_even_when_the_work_explodes(self, worker, args, monkeypatch):
        def boom(*a, **k):
            raise TypeError("'NoneType' object is not iterable")

        for name in ("add_songs_to_playlists", "export_playlists_to_csv", "fetch_playlists"):
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

        monkeypatch.setattr(youtube, "add_songs_to_playlists", boom)
        stub = Stub()
        CratefillApp._worker(stub, FakeYT(), [SONG], [PLAYLIST])
        logs = kinds(stub.drain(), "log")
        assert any("Unexpected error while adding: TypeError" in line for line in logs)

    def test_add_refetches_playlists_so_counts_stay_current(self):
        stub, yt = Stub(), FakeYT()
        CratefillApp._worker(stub, yt, [SONG], [PLAYLIST])
        messages = stub.drain()
        assert kinds(messages, "playlists")           # refetched on the worker thread
        assert messages[-1] == ("done", None)
