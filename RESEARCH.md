# Cratefill — CSV to YouTube Music Playlist

**Goal:** Automatically create a playlist in a YouTube Music account from a CSV file containing "artist + song name" tracks.

*Research date: 2026-06-11*

---

## Option 1 — ytmusicapi (unofficial Python library) ⭐ Recommended

- **Repo:** https://github.com/sigma67/ytmusicapi
- **Docs:** https://ytmusicapi.readthedocs.io/
- **PyPI:** https://pypi.org/project/ytmusicapi/ (v1.12.1, released 2026-06-05 — actively maintained)

Emulates the YouTube Music web client, so:

- **No API quota** — unlimited searches and inserts.
- **Searches the YouTube Music catalog** (actual songs), not general YouTube videos → much better match quality than the official API.

### Workflow (~20 lines of code)

1. Read the CSV.
2. For each row: `yt.search("artist title", filter="songs")` → take top hit's `videoId`.
3. `yt.create_playlist(name, description)` then `yt.add_playlist_items(playlistId, videoIds)`.

### Authentication (the only fiddly part)

| Method | Setup | Caveats |
|---|---|---|
| **Browser auth** | Copy request headers from an authenticated music.youtube.com tab (~2 min) | Expires if you log out of that browser session; does support uploads |
| **OAuth** | Create a free Google Cloud project + "TV and Limited Input devices" OAuth client | More setup; credentials self-refresh; does **not** work for uploads (not needed here) |

### Ready-made wrappers (if avoiding custom code)

- https://github.com/akbak/csv2ymusic
- https://github.com/bcherb2/youtube-music-importer
- https://github.com/Coleslaw3557/csv-to-yt-music — retry logic; expects Title/Artist/Album columns

A custom script is still preferable for controlling match quality (e.g., log fuzzy/ambiguous matches for manual review).

---

## Option 2 — Official YouTube Data API v3

- **Quota calculator:** https://developers.google.com/youtube/v3/determine_quota_cost
- **playlistItems.insert:** https://developers.google.com/youtube/v3/docs/playlistItems/insert

The officially supported route, but poorly suited here:

- Each track costs ~150 quota units: `search.list` = 100 + `playlistItems.insert` = 50.
- Default daily quota = 10,000 units → **~65 tracks per day**.
- Search returns general YouTube videos (lyric videos, covers) rather than canonical YouTube Music songs.

Only worth it for an officially supported / production integration.

---

## Option 3 — Web transfer services (zero code)

| Service | CSV import | Free limit |
|---|---|---|
| [TuneMyMusic](https://www.tunemymusic.com/transfer/csv-to-youtube-music) | Yes | **500 tracks** |
| [Soundiiz](https://soundiiz.com/tutorial/import-excel-to-youtube-music) | Yes ([format docs](https://support.soundiiz.com/hc/en-us/articles/360010006793-What-is-the-CSV-format-to-import-playlists-and-favorites)) | **200 tracks** per playlist |

Fastest for a one-off under the free cap. Downsides: third party gets account access; no control over how ambiguous tracks are matched.

---

## Recommendation

- **One-off, <500 tracks:** TuneMyMusic (~5 minutes).
- **Repeatable / full control:** Python script on **ytmusicapi** — free, no quota, can report fuzzy matches for review. This is the planned approach for this project.

## Next steps

1. Confirm CSV format (assumed: two columns, `artist,title`).
2. Set up ytmusicapi auth (browser auth is quickest).
3. Write script: CSV → search → create playlist → add items, with a report of tracks that didn't match cleanly.
