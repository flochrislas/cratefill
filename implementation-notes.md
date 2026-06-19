# Cratefill — Implementation Notes

Notes for a developer taking over the project. Read `README.md` first for what
the app does from a user's point of view; this file explains how it's built and
why. `RESEARCH.md` documents the alternatives that were considered before
settling on this approach.

*Last updated: 2026-06-19 — matches `cratefill.py` as of that date (v0.1.0).*

## Stack and key decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3 (developed on 3.14) | ytmusicapi is a Python library |
| YouTube Music access | [ytmusicapi](https://github.com/sigma67/ytmusicapi) (unofficial) | No API quota; searches the actual YT Music song catalog. The official YouTube Data API v3 costs ~150 quota units per track (≈65 tracks/day on the default 10k quota) and searches all of YouTube, not just music — see `RESEARCH.md` |
| GUI | Tkinter (`ttk` widgets) | Ships with Python — no packaging issues on Windows |
| Theme | Hand-rolled dark theme on built-in `clam` (`apply_dark_theme()`) | `clam` is the one built-in ttk theme that renders identically on Windows and Linux, so the dark UI is cross-platform with zero dependencies. sv-ttk was tried first and abandoned: on this Python 3.14 / Tk 8.6.15 build it registered its theme name but applied empty style settings (half-light UI). Palette lives in module constants (`BG`, `FIELD`, `BTN`, `FG`, `ACCENT`…); plain tk widgets (Text, Listbox) aren't covered by ttk themes and take `DARK_LIST_STYLE`/`DARK_TEXT_STYLE` directly. The title bar is darkened via `enable_dark_title_bar()` (Windows DWM attribute, best-effort no-op elsewhere; Linux title bars follow the desktop's window manager theme) |
| Architecture | Single file, `cratefill.py` | Small enough (~780 lines); split only if it grows |
| Auth persistence | `browser.json` next to the script | ytmusicapi's standard browser-auth file format |

Everything lives in `cratefill.py`. There is no database. For day-to-day dev,
`pip install ytmusicapi` and run; the only build-time artifact is the
distribution metadata in `pyproject.toml` (see **Packaging & releasing**).

**Dev environment note:** on the original dev machine the bare `python` command
is a broken Windows Store shim — use the `py` launcher.

## Code structure (`cratefill.py`)

```
module level
├── constants: APP_DIR, AUTH_FILE, ARTIST/TITLE/STATION_HEADERS, SONG_COLUMNS,
│                LOGIN_INSTRUCTIONS
├── read_songs_csv(path)        # CSV → list[(artist, title, station)]
├── read_songs_folder(path)     # music files → list[(folder, file stem, "")]
├── clean_pasted_headers(raw)   # DevTools paste → {header: value}
├── normalize(s) / pick_match() # search-result matching heuristic
├── safe_filename(name)         # playlist title → legal file name
├── write_playlist_csv(...)     # get_playlist tracks → Artist/Title/Album CSV
├── class LoginDialog(Toplevel) # paste-headers auth dialog
├── class CratefillApp         # main window + all behavior
└── main()
```

### `read_songs_csv(path)` — CSV ingestion

Pure function, independently testable (no GUI/network). Handles real-world CSV
messiness in this order:

1. **Encoding:** tries `utf-8-sig` (eats Excel's BOM), then `cp1252` (legacy
   Windows/French Excel), then UTF-8 with replacement characters as last resort.
2. **Delimiter:** `csv.Sniffer` over the first 4 KB, restricted to `,` `;` `\t`
   (semicolon matters: French-locale Excel exports use it). Falls back to comma.
3. **Column mapping:** if the first row contains a header matching
   `ARTIST_HEADERS` *and* one matching `TITLE_HEADERS` (English + French names,
   casefolded), those columns are used and the header row dropped. Otherwise
   columns 0/1 are assumed (artist, title); a first row that contains *any*
   known header name is still dropped as a probable header. A column matching
   `STATION_HEADERS` ("where I heard this") is picked up too, but **only by
   header name** — never positionally — and yields `""` when absent. The
   station is shown in the UI for the user's benefit and ignored when
   searching YouTube Music.
4. Blank rows and rows too short for the mapped columns are skipped.

To support new header names, just extend the tuples at the top of the file.

### `pick_match(results, artist, title)` — match heuristic

Given ytmusicapi search results, returns `(result, confident)`:

- **Confident match:** normalized title is a substring of the result title (or
  vice versa) *and* the normalized artist matches one of the result's artists
  the same way. Normalization = casefold + strip non-alphanumerics.
- **Fallback:** first result that has a `videoId`, flagged `confident=False`.
  These show as `?` lines in the log with what was actually found, so the user
  can review; they are still added to the playlist.
- `(None, False)` if nothing usable.

Substring matching is deliberately loose — it tolerates "(Radio Edit)",
"feat. X" etc. If match quality becomes a problem, this is the function to
improve (e.g. `difflib.SequenceMatcher` ratio, or duration comparison if the
CSV ever carries durations). Note `videoId` can be `None` on some result types,
hence the filter.

### Authentication flow

YouTube Music has no public login API. The app uses **ytmusicapi browser auth**:
the user copies the request headers of an authenticated `music.youtube.com`
`/browse` request from their browser's dev tools and pastes them into
`LoginDialog`. The dialog then:

1. `clean_pasted_headers(raw)` — normalizes the paste into a `{name: value}`
   dict. Needed because Chrome/Edge's headers panel copies names and values on
   alternating lines, with HTTP/2 pseudo-headers (`:authority`…) and the
   decoded `x-client-data` protobuf block mixed in; ytmusicapi's own parser
   desyncs on that and writes bogus headers (e.g. a request path as a header
   name) into `browser.json`, which makes YouTube reject every request with a
   non-JSON body ("Expecting value: line 1 column 1"). The dialog also errors
   early if no `cookie` was found, and defaults `x-goog-authuser` to `0`.
2. `ytmusicapi.setup(filepath=AUTH_FILE, headers_raw=...)` — fed the cleaned
   `name: value` lines, writes `browser.json`.
3. Validates by calling `get_library_playlists(limit=1)`; on failure the file
   is deleted and an error shown (so a bad paste never leaves a broken
   `browser.json` behind).

On startup, if `browser.json` exists, `_connect(silent=True)` reuses it.
Sessions die when the user logs out of YouTube in that browser, or after some
months; symptom is `get_library_playlists` raising — surfaced in the log as
"Login expired? Re-log in."

ytmusicapi also supports OAuth (Google Cloud project + "TV and Limited Input"
client). It was skipped because the setup burden is on the end user; if header
pasting proves too painful, that's the alternative — see
https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html

**`browser.json` contains the user's session cookies. Never commit it.** (It
is in `.gitignore`, together with patterns for other auth-file variants.)

### Threading model

Tkinter is single-threaded; network calls would freeze the UI. The pattern used:

- The **Add** button (`add_songs`) and **Export CSV…** button
  (`export_playlists`) snapshot the selections, disable both buttons
  (`_start_work`), and start a daemon `threading.Thread` running `_worker` /
  `_export_worker` respectively.
- The workers do all network I/O and communicate *only* by putting
  `(kind, payload)` tuples on `self.worker_queue` (a `queue.Queue`). Kinds:
  `"log"` (a line for the log pane), `"step"` (advance progress bar), and
  `"done"` — with payload `"refresh"` when the playlists should be refetched
  (after adding; pointless after an export).
- `_poll_worker`, rescheduled every 100 ms via `root.after`, drains the queue
  on the main thread and touches the widgets.

**Rule: no Tk widget is ever touched from the worker thread.** Keep it that way
— violating it causes intermittent crashes that are miserable to reproduce.
The only ytmusicapi calls *not* on a worker thread are login validation and
`refresh_playlists`; they're quick single requests and were left synchronous
for simplicity (acceptable freeze of <1 s; move them to the worker pattern if
that ever bothers anyone).

`self.working` guards against double-starting a job.

### The add operation (`_worker`)

Two phases, on purpose:

1. **Search phase:** one `yt.search(f"{artist} {title}", filter="songs",
   limit=5)` per song; collect matched `videoId`s. Per-song failures (search
   exception, no match) are logged and *don't* abort the run.
2. **Add phase:** one `yt.add_playlist_items(playlistId, video_ids,
   duplicates=False)` call **per playlist** with all matched IDs batched — not
   one call per song, which would be slow and rate-limit-prone.

`duplicates=False` does **not** make YT Music skip songs already in the
playlist — it makes the whole batch fail atomically (nothing added) if even
one song is a duplicate, and ytmusicapi's `duplicates=True` would add the
duplicates. So on a failed status, `_worker` fetches the playlist's current
videoIds, filters them out of the batch, and retries once with the rest
(logging "N already there, skipped"); matched videoIds are also deduped
within the batch. If the retry still fails (playlist not editable, or YT
considers a song a duplicate under a *different* videoId), a soft warning is
logged. Adding to a playlist the user doesn't own fails per-playlist and is
logged without affecting the others.

After completion, `refresh_playlists()` runs so track counts update.

### Folder → playlist (`load_folder`)

Same flow as CSV loading; `read_songs_folder` fills the artist column with the
folder name and the title column with each music file's stem (extensions per
`AUDIO_EXTENSIONS`, non-recursive), so the search query becomes "folder name +
file name" with no other code changes. Works best for folders named after an
artist/album; for station folders of "Artist - Title.ext" files most matches
land as `?` (uncertain) because the folder name isn't the artist — still
useful, just review the log.

### Playlist → CSV export (`_export_worker`)

One `yt.get_playlist(playlistId, limit=None)` per selected playlist, then
`write_playlist_csv` writes `Artist,Title,Album` rows (UTF-8, csv module
quoting). Playlist titles are made filesystem-safe by `safe_filename`, and
existing files get a ` (2)` suffix instead of being overwritten. Per-playlist
failures are logged and don't abort the run. The exported CSV round-trips
through `read_songs_csv` (the Album header is deliberately *not* in
`STATION_HEADERS`).

### UI layout

`ttk.PanedWindow` with two resizable panes, plus a bottom strip:

- **Left:** `ttk.Treeview` (columns artist/title/station per `SONG_COLUMNS`,
  `selectmode="extended"`; the station column is hidden via `displaycolumns`
  when the CSV has no stations). Row iids are the **index into `self.songs`**
  as a string — that's how selections map back to data. Clicking a column
  heading calls `sort_songs(col)`, which only *reorders rows with
  `tree.move`* (and toggles a ▲/▼ heading arrow); iids never change, so the
  mapping and the current selection survive sorting. If you ever add
  filtering, preserve that invariant.
- **Right:** `tk.Listbox` (`selectmode="extended"`, `exportselection=False` —
  without that flag, clicking the other pane silently clears the selection).
  Indices map directly into `self.playlists`.
- **Bottom:** Add button, determinate `ttk.Progressbar` (max = songs +
  playlists, one step per unit of work), and a read-only `tk.Text` log.

Dropping a CSV file or a music folder onto the song tree loads it
(`_on_drop`, routed to `load_csv_path`/`load_folder_path` — the same methods
the buttons use). This needs the optional `tkinterdnd2` package and the
`TkinterDnD.Tk()` root that `main()` creates when the package is present;
without either, the app degrades to buttons-only (the import is guarded and
`_build_ui` ignores the `TclError` raised when registering a drop target on a
plain `tk.Tk()` root — which is what the smoke test and screenshot helper use).

## Packaging & releasing

Distribution metadata is in `pyproject.toml` (setuptools backend, single
`py-modules = ["cratefill"]` — the app stays one file). `ytmusicapi` is a hard
dependency; `tkinterdnd2` is the optional `[dnd]` extra. The console entry point
is `[project.gui-scripts] cratefill = "cratefill:main"` (`gui-scripts`, not
`scripts`, so Windows launches it without a console window). The version is
declared in **two** places that must match: `__version__` in `cratefill.py` and
`version` in `pyproject.toml`.

Two deliverables per release:

1. **PyPI** (`pip install cratefill`). Built with `py -m build` (sdist + wheel),
   validated with `py -m twine check dist/*`. Publishing is automated:
   `.github/workflows/publish.yml` runs on any pushed `v*` tag and uploads via
   GitHub Actions **OIDC trusted publishing** — no API token is stored anywhere.
   The matching PyPI-side publisher config (owner `flochrislas`, repo
   `cratefill`, workflow `publish.yml`, environment `pypi`) is a one-time setup
   already in place. Manual `twine upload` (with a `pypi-…` token) remains a
   fallback, but must run in an interactive terminal — twine prompts for the
   token and PyPI has no web upload.

2. **Standalone Windows `.exe`**, attached to the GitHub release. Built with
   `py -m PyInstaller --onefile --windowed --name Cratefill --collect-all
   tkinterdnd2 cratefill.py`. The `--collect-all tkinterdnd2` is essential: it
   bundles the native `tkdnd` binaries, without which the frozen app raises at
   `TkinterDnD.Tk()` in `main()` and won't start. This step is **not** in CI
   (it needs a Windows runner) — build locally and `gh release create` with the
   exe. The README's screenshot is a committed `docs/screenshot.png` referenced
   by absolute raw-GitHub URL so it renders on the PyPI project page too.

`build/`, `dist/`, and `*.spec` are gitignored.

## Testing

There is no test suite yet. What was verified at build time:

- `read_songs_csv`: header detection (`sample.csv`), French semicolon CSV with
  accents, headerless two-column CSV.
- UI construction: `root = tk.Tk(); root.withdraw(); CratefillApp(root);
  root.update(); root.destroy()` — catches widget-level errors headlessly.
- **Not** verified automatically: anything that hits YouTube Music (needs a
  real session). Test manually with `sample.csv` and a throwaway playlist.

`read_songs_csv` and `pick_match` are pure and are the natural first targets
if a pytest suite is added.

## Known limitations / ideas for whoever takes over

- **No retry/rate-limit handling** on search. Fine for tens of songs; for
  hundreds, add a small delay or retry-on-exception in the search loop.
- **Uncertain matches are auto-added.** A nicer flow: collect `?` matches and
  show a confirmation dialog before adding.
- **No playlist creation** from the app — users must create the playlist on
  YT Music first. `yt.create_playlist(title, description)` makes this a small
  feature (button + name prompt + refresh).
- **Header-paste login is the main UX pain point.** Options: OAuth flow, or
  guiding screenshots in the dialog.
- **`get_library_playlists(limit=None)`** fetches everything; fine up to
  hundreds of playlists.
- ytmusicapi is unofficial and tracks YT Music's private web API — a YT-side
  change can break it. First debugging step for sudden breakage: upgrade the
  library (`py -m pip install -U ytmusicapi`) and check its GitHub issues.
