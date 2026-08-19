"""Tests for local file handling: CSV in/out, folders, filenames. No Tk, no network."""

import csv
from pathlib import Path

import pytest

from cratefill.storage import (
    read_songs_csv,
    read_songs_folder,
    safe_filename,
    write_playlist_csv,
)

# Resolved from this file, not the working directory, so pytest can run anywhere.
SAMPLE_CSV = Path(__file__).resolve().parent.parent / "sample.csv"


def write(tmp_path, name, text, encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


class TestReadSongsCsv:
    def test_sample_file_ships_and_parses(self):
        """sample.csv is the file users are pointed at in the README."""
        assert read_songs_csv(SAMPLE_CSV) == [
            ("Daft Punk", "Harder Better Faster Stronger", "FIP"),
            ("Phoenix", "Lisztomania", "Radio Nova"),
            ("Air", "La Femme d'Argent", "FIP"),
        ]

    def test_english_headers(self, tmp_path):
        p = write(tmp_path, "a.csv", "Artist,Title\nPhoenix,Lisztomania\n")
        assert read_songs_csv(p) == [("Phoenix", "Lisztomania", "")]

    def test_french_headers_and_semicolons(self, tmp_path):
        p = write(tmp_path, "fr.csv", "Artiste;Titre;Chaîne\nAir;La Femme d'Argent;FIP\n")
        assert read_songs_csv(p) == [("Air", "La Femme d'Argent", "FIP")]

    def test_tab_delimiter(self, tmp_path):
        p = write(tmp_path, "t.csv", "Artist\tSong\nPhoenix\tLisztomania\n")
        assert read_songs_csv(p) == [("Phoenix", "Lisztomania", "")]

    def test_headerless_uses_first_two_columns(self, tmp_path):
        p = write(tmp_path, "h.csv", "Phoenix,Lisztomania\nAir,Sexy Boy\n")
        assert read_songs_csv(p) == [("Phoenix", "Lisztomania", ""), ("Air", "Sexy Boy", "")]

    def test_utf8_bom_is_eaten(self, tmp_path):
        """Excel writes a BOM; it must not end up glued to the first header."""
        p = write(tmp_path, "bom.csv", "Artist,Title\nPhoenix,Lisztomania\n", encoding="utf-8-sig")
        assert read_songs_csv(p) == [("Phoenix", "Lisztomania", "")]

    def test_cp1252_fallback(self, tmp_path):
        """Legacy Windows/French Excel exports aren't UTF-8."""
        p = tmp_path / "cp.csv"
        p.write_bytes("Artiste;Titre\nAir;La Femme d'Argent\n".encode("cp1252"))
        assert read_songs_csv(p) == [("Air", "La Femme d'Argent", "")]

    def test_extra_columns_ignored(self, tmp_path):
        p = write(tmp_path, "x.csv", "Artist,Title,Year,Note\nPhoenix,Lisztomania,2009,fun\n")
        assert read_songs_csv(p) == [("Phoenix", "Lisztomania", "")]

    def test_station_is_matched_by_header_name_only(self, tmp_path):
        """A third column that isn't a known station header must stay empty —
        never picked up positionally."""
        p = write(tmp_path, "s.csv", "Artist,Title,Year\nPhoenix,Lisztomania,2009\n")
        assert read_songs_csv(p) == [("Phoenix", "Lisztomania", "")]

    def test_quoted_field_with_comma(self, tmp_path):
        p = write(tmp_path, "q.csv", 'Artist,Title\n"Tyler, The Creator",EARFQUAKE\n')
        assert read_songs_csv(p) == [("Tyler, The Creator", "EARFQUAKE", "")]

    def test_blank_and_short_rows_are_skipped(self, tmp_path):
        p = write(tmp_path, "b.csv", "Artist,Title\nPhoenix,Lisztomania\n\n,\nOnlyArtist\n")
        assert read_songs_csv(p) == [("Phoenix", "Lisztomania", "")]


class TestReadSongsFolder:
    def test_uses_folder_name_as_artist_and_stem_as_title(self, tmp_path):
        artist_dir = tmp_path / "Phoenix"
        artist_dir.mkdir()
        (artist_dir / "Lisztomania.mp3").touch()
        assert read_songs_folder(artist_dir) == [("Phoenix", "Lisztomania", "")]

    def test_is_not_recursive(self, tmp_path):
        """Only files directly inside the chosen folder count — its name is the
        artist, so descending into subfolders would attribute them wrongly."""
        (tmp_path / "Sub").mkdir()
        (tmp_path / "Sub" / "Nested.mp3").touch()
        assert read_songs_folder(tmp_path) == []

    def test_ignores_non_audio_files(self, tmp_path):
        (tmp_path / "cover.jpg").touch()
        (tmp_path / "notes.txt").touch()
        (tmp_path / "Sexy Boy.flac").touch()
        assert [t for _, t, _ in read_songs_folder(tmp_path)] == ["Sexy Boy"]

    def test_empty_folder(self, tmp_path):
        assert read_songs_folder(tmp_path) == []


class TestSafeFilename:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Road trip", "Road trip"),
            ("AC/DC", "AC_DC"),
            ('a<b>c:d"e|f?g*h', "a_b_c_d_e_f_g_h"),
            ("trailing dots...", "trailing dots"),
            ("  ", "playlist"),      # nothing usable left
            ("///", "___"),          # replaced, not emptied — still a legal name
        ],
    )
    def test_sanitizes(self, raw, expected):
        assert safe_filename(raw) == expected


class TestWritePlaylistCsv:
    def track(self, title="Lisztomania", artists=("Phoenix",), album="Wolfgang"):
        return {
            "title": title,
            "artists": None if artists is None else [{"name": a} for a in artists],
            "album": None if album is None else {"name": album},
        }

    def test_writes_artist_title_album(self, tmp_path):
        path = write_playlist_csv("Road trip", [self.track()], tmp_path)
        assert path.name == "Road trip.csv"
        rows = list(csv.reader(path.open(encoding="utf-8")))
        assert rows == [["Artist", "Title", "Album"], ["Phoenix", "Lisztomania", "Wolfgang"]]

    def test_joins_multiple_artists(self, tmp_path):
        path = write_playlist_csv("x", [self.track(artists=("Air", "Phoenix"))], tmp_path)
        assert "Air, Phoenix" in path.read_text(encoding="utf-8")

    def test_missing_artists_and_album(self, tmp_path):
        path = write_playlist_csv("x", [self.track(artists=None, album=None)], tmp_path)
        rows = list(csv.reader(path.open(encoding="utf-8")))
        assert rows[1] == ["", "Lisztomania", ""]

    def test_sanitizes_the_playlist_title(self, tmp_path):
        assert write_playlist_csv("AC/DC", [], tmp_path).name == "AC_DC.csv"

    def test_never_overwrites_an_existing_file(self, tmp_path):
        first = write_playlist_csv("Road trip", [], tmp_path)
        second = write_playlist_csv("Road trip", [], tmp_path)
        third = write_playlist_csv("Road trip", [], tmp_path)
        assert [p.name for p in (first, second, third)] == [
            "Road trip.csv",
            "Road trip (2).csv",
            "Road trip (3).csv",
        ]
