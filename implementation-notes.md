# Cratefill — Implementation Notes

Notes for a developer taking over the project. Read `README.md` first for what
the app does from a user's point of view; this file explains how it's built and
why. `RESEARCH.md` documents the alternatives that were considered before
settling on this approach.

*Last updated: 2026-08-19 — matches the `cratefill/` package as of that date (v0.1.1).*

## Stack and key decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3 (developed on 3.14) | ytmusicapi is a Python library |
| YouTube Music access | [ytmusicapi](https://github.com/sigma67/ytmusicapi) (unofficial) | No API quota; searches the actual YT Music song catalog. The official YouTube Data API v3 costs ~150 quota units per track (≈65 tracks/day on the default 10k quota) and searches all of YouTube, not just music — see `RESEARCH.md` |
| GUI | Tkinter (`ttk` widgets) | Ships with Python — no packaging issues on Windows |
| Theme | Hand-rolled dark theme on built-in `clam` (`apply_dark_theme()`) | `clam` is the one built-in ttk theme that renders identically on Windows and Linux, so the dark UI is cross-platform with zero dependencies. sv-ttk was tried first and abandoned: on this Python 3.14 / Tk 8.6.15 build it registered its theme name but applied empty style settings (half-light UI). Palette lives in module constants (`BG`, `FIELD`, `BTN`, `FG`, `ACCENT`…); plain tk widgets (Text, Listbox) aren't covered by ttk themes and take `DARK_LIST_STYLE`/`DARK_TEXT_STYLE` directly. The title bar is darkened via `enable_dark_title_bar()` (Windows DWM attribute, best-effort no-op elsewhere; Linux title bars follow the desktop's window manager theme) |
| Architecture | Small package, `cratefill/` (`app`, `matching`, `storage`, `youtube`) | Started as one file; split at ~1100 lines because UI layout, matching rules, local file handling and YouTube Music communication change for unrelated reasons, and the improved matching system needs a pure, testable core. Kept deliberately modest — no `utils.py`, no module-per-class |
| Song matching | Token-aware scoring on top of `rapidfuzz`, with independent artist/title thresholds and version-marker checks | Substring matching accepted `one → someone` and `cher → cherub`, and the old first-result fallback added unrelated songs silently. `rapidfuzz` gives order-tolerant scorers (`token_set_ratio`, `WRatio`) far better than `difflib` and is fast enough to be irrelevant at these sizes; the fuzzy call is isolated in `_ratio()` so it can be swapped |
| Ambiguous matches | User policy in `settings.json` (`ask`/`skip`/`add`, default `ask`), applied by `policy.py` | Matching can only say how credible a candidate is; what to do about "credible but uncertain" is a user preference. Kept out of `matching.py` so the rules stay pure and testable, and out of `browser.json` so a preference never shares a file with session cookies |
| Auth persistence | `browser.json` in the per-user data dir (`user_data_dir()`) | ytmusicapi's standard browser-auth file format. Kept out of the app directory: that can be read-only for system-wide installs, is wiped on every launch under PyInstaller `--onefile`, and invites copying session cookies around with the executable. Created `0600` in a `0700` directory on POSIX, since ytmusicapi writes it with the process umask |

There is no database. For day-to-day dev, `pip install -e ".[dev]"` and run
`py -m cratefill`; the only build-time artifact is the distribution metadata in
`pyproject.toml` (see **Packaging & releasing**).

**Dev environment note:** on the original dev machine the bare `python` command
is a broken Windows Store shim — use the `py` launcher.

## Code structure (`cratefill/`)

```
cratefill/
├── __init__.py    __version__ only — the single source of the version, and kept
│                  import-light because setuptools reads the attribute
├── __main__.py    python -m cratefill → app.main()
├── matching.py    normalize/tokens/core_title, version_markers,
│                  score_text/score_artist, choose_match → MatchDecision
│                  pure: re + unicodedata + rapidfuzz, no I/O, deterministic
├── policy.py      POLICIES ("ask"/"skip"/"add"), load_policy/save_policy,
│                  action_for_match(decision, policy) → "add"/"skip"/"ask"
├── storage.py     user_data_dir()            per-user data directory
│                  read_json/write_json_atomic  settings file mechanics
│                  read_songs_csv(path)       CSV → list[(artist, title, station)]
│                  read_songs_folder(path)    music files → list[(folder, stem, "")]
│                  safe_filename(name)        playlist title → legal file name
│                  write_playlist_csv(...)    tracks → Artist/Title/Album CSV
│                  ARTIST/TITLE/STATION_HEADERS, AUDIO_EXTENSIONS
├── youtube.py     AUTH_FILE, LEGACY_AUTH_FILE, secure_auth_dir/file(),
│                  migrate_legacy_auth_file()
│                  clean_pasted_headers(raw)  DevTools paste → {header: value}
│                  open_session()             saved session → YTMusic client
│                  save_credentials(headers)  stage, validate, atomically install
│                  fetch_playlists(yt, put)
│                  search_candidates(yt, artist, title)
│                  evaluate_songs(yt, songs, put) → [(song, MatchDecision)]
│                  add_video_ids_to_playlists(yt, ids, playlists, put)
│                  export_playlists_to_csv(yt, playlists, dest, put)
└── app.py         palette + apply_dark_theme(), enable_dark_title_bar()
                   SONG_COLUMNS, LOGIN_INSTRUCTIONS, HELP_TEXT
                   class LoginDialog(Toplevel)           paste-headers auth dialog
                   class AmbiguousMatchDialog(Toplevel)  Skip/Add + remember
                   class CratefillApp           window, selections, threads, queue
                   main()

run_cratefill.py   PyInstaller entry script (see PyInstaller section)
tests/             test_matching.py, test_policy.py, test_storage.py,
                   test_workers.py, test_review.py, test_dialog.py
```

Dependency direction is one-way:
`__main__ → app → {youtube → {matching, storage}, policy → storage}`. Nothing
imports `app`. `matching.py` imports only `re`, `unicodedata` and `rapidfuzz`;
`storage.py`, `policy.py` and `youtube.py` never import Tkinter; `app.py` makes
no `yt.*` call of its own; and `matching.py` knows nothing about the ambiguous
policy. Those properties are what keep the test suite free of Tk and network, and
they are worth asserting in review.

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

### `choose_match(artist, title, results)` — the matching pipeline

Returns a `MatchDecision` whose `status` is one of:

| status | meaning | what happens |
|---|---|---|
| `high` | artist and title both match strongly, no version conflict | added |
| `ambiguous` | credible, but not certain | the user's policy decides |
| `rejected` | failed a minimum, or a conflicting recording | always skipped |

**This replaced a much looser heuristic**, and the replacement is the point of
the design. The old version compared substrings and, when nothing matched,
returned the first search result anyway — flagged `?` but added regardless. That
made `Cher — One` silently become `Cherub — Someone`, and turned studio requests
into live versions, remixes and karaoke tracks. Two rules now prevent that:

* **There is no fallback.** Nothing credible → `rejected`, and no policy setting
  can override it.
* **Artist and title are thresholded independently** (`MIN_ARTIST`, `MIN_TITLE`)
  before the weighted `overall_score` is used at all, so a perfect title can
  never carry the wrong artist, or vice versa.

The stages, all in `matching.py`:

1. **Validate the row** — `validate_request()` refuses an empty title or artist.
   `youtube.evaluate_songs` calls it *before* searching, so a hopeless row costs
   no network call.
2. **Search several candidates** — `SEARCH_LIMIT = 10`, and every result with a
   `videoId` is scored. Result order is not trusted.
3. **Normalize** — `normalize()` does NFKD accent stripping (`Beyoncé → beyonce`),
   curly-quote flattening, punctuation → space (`AC/DC → ac dc`), whitespace
   collapsing, plus two special cases: a `_LIGATURES` table for letters NFKD
   won't split (`Cœur → coeur`, `ß → ss`) and interior `!`/`$` → `i`/`s` for
   stylised names (`P!nk → pink`, `Ke$ha → kesha`).
4. **Keep version information** — `version_markers()` reads the *whole* title,
   brackets included. Hard markers are live, remix, acoustic, instrumental,
   karaoke, cover, demo, radio edit, extended, sped up, slowed, clean, explicit.
   Soft markers (remastered, deluxe, anniversary, album version, official video…)
   are stripped by `core_title()`, which is why `Wonderwall` still matches
   `Wonderwall (Remastered)`. `core_title()` also drops `feat. X`.

   `version_relation()` compares hard markers **asymmetrically**, because the two
   directions are not equally bad:

   | relation | example | outcome |
   |---|---|---|
   | `same` | ask *Wonderwall (Live)*, get *Wonderwall (Live)* | can be `high` |
   | `extra` | ask *Wonderwall*, get *Wonderwall (Live)* | `rejected` |
   | `missing` | ask *Wonderwall (Live)*, get *Wonderwall* | capped at `ambiguous` |

   The `missing` case is a deliberate concession: if the live version simply
   isn't on YouTube Music, offering the album version beats returning nothing —
   but it is never silent, so it can only ever be ambiguous. `extra` stays a
   hard refusal, and so does a candidate whose markers merely *differ* (asking
   for live and being handed a remix is `extra`, not a fallback).

   Ranking keeps the two apart: exact-version candidates are sorted ahead of
   fallbacks, and the runner-up margin is computed within a group. A fallback is
   strictly inferior, so it must neither outrank the requested recording nor make
   it look like a near tie — it is still listed in `alternatives`.
5. **Compare whole tokens** — `score_text()` scores 1.0 for equal token sets;
   otherwise the token sets must genuinely intersect before any fuzzy score is
   trusted. That gate is what kills `one → someone` and `cher → cherub`, where
   character similarity is high but no whole word is shared. Overlap is divided
   by the **longer** side, so `hello` can't pass as `hello world goodbye` either.
   Values of four characters or fewer get no fuzzy credit at all. The fuzzy part
   is `max(rapidfuzz.fuzz.token_set_ratio, WRatio) / 100`, isolated in `_ratio()`.
6. **Score the artist** — `score_artist()` takes the best score across every
   returned artist, ignoring nameless/malformed entries, strips a leading "the"
   (`The Beatles ≡ Beatles`), and lets requested `feat.` guests match any
   returned artist. Extra artists on the result never penalise it.
7. **Rank and classify** — candidates failing a minimum or conflicting on version
   are dropped *before* ranking, so a live variant alongside the studio one
   doesn't make the result ambiguous. `high` additionally needs `HIGH_TITLE` /
   `HIGH_ARTIST` and to beat the runner-up by `WINNER_MARGIN`; otherwise
   `ambiguous`, with `reasons` explaining which condition failed.

All thresholds are module constants at the top of the file, deliberately strict:
a false negative costs a prompt, a false positive puts the wrong song in a
playlist. Loosen them only with real examples and tests.

ytmusicapi is unofficial, so every field is treated as optional: `results` itself
may be `None`, entries may not be dicts, `artists` may be missing/`null`/hold
entries without a `name`, and `title` may be absent. `normalize()` maps `None` to
`""` for that reason. This is not hypothetical — a result with `artists=None`
used to raise `TypeError` and take down the whole worker thread with it.

### Ambiguous-match policy (`policy.py`)

`matching.py` deliberately does not know what an ambiguous result should *lead
to*. `policy.action_for_match(decision, saved)` makes that call and returns
`"add"`, `"skip"` or `"ask"`:

```python
high     → "add"    whatever the setting says
rejected → "skip"   whatever the setting says
ambiguous→ the saved policy ("ask" | "skip" | "add")
```

The asymmetry is the safety property: **"Always add" means "accept credible but
uncertain candidates", never "add the first unrelated search result"**.

The setting is stored as `{"ambiguous_match_policy": "ask"}` in `settings.json`
in `storage.user_data_dir()` — beside `browser.json`, never inside it: a
preference has no business sharing a file with session cookies. Writes go through
`storage.write_json_atomic()` (staged sibling + `os.replace`, same reasoning as
`save_credentials`) and preserve unrelated keys, so a future version's settings
survive an older version writing. Anything wrong with the file — absent,
unreadable, malformed, not an object, unknown value — falls back to `ask`.

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
2. `ytmusicapi.setup(filepath=…, headers_raw=…)` — fed the cleaned
   `name: value` lines. It writes to a **staged** sibling file from
   `tempfile.mkstemp(dir=AUTH_FILE.parent)`, never to the live `browser.json`.
3. Validates the staged file by calling `get_library_playlists(limit=1)`, then
   `os.replace()`s it onto `browser.json` — atomic, because staging in the same
   directory guarantees the same filesystem. On failure the staged file is
   removed and an error shown, leaving any existing session untouched: a
   mistyped re-login must not cost the user a working session (it used to,
   because setup() overwrote the live file and the error path deleted it).
   `mkstemp` also creates the staged file `0600`, so the new credentials are
   never briefly world-readable.

Steps 2–3 are a network round trip, so they run in `_validate_worker` on its own
thread; the dialog keeps its own `result_queue` and polls it with
`_poll_validation` (`after(100, …)`), showing "Checking with YouTube Music…" and
disabling both buttons meanwhile. `self.validating` also blocks the window's
close button, so the dialog can't be destroyed while a thread still owns the
staged file. `_poll_validation` re-checks `winfo_exists()` before touching
widgets.

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

It lives in the per-user data directory returned by `user_data_dir()` —
`%APPDATA%\Cratefill` on Windows, `~/Library/Application Support/Cratefill` on
macOS, `$XDG_CONFIG_HOME/cratefill` (default `~/.config/cratefill`) elsewhere.
Three helpers guard it: `secure_auth_dir()` creates the directory `0700`,
`secure_auth_file()` chmods the file to `0600` (ytmusicapi writes it through
plain `open(..., "w")`, so its mode is whatever the umask allows — commonly
world-readable), and `migrate_legacy_auth_file()` moves an older file from
beside the script/exe on startup so upgrading doesn't force a re-login and no
readable copy is left behind. The migration copies via
`os.open(..., 0o600)` rather than `Path.replace`/`shutil.move`: the old and new
locations are usually on different filesystems, and this way the new file is
never briefly readable by other users.

### Threading model

Tkinter is single-threaded; network calls would freeze the UI. The pattern used:

- The **Add** button (`add_songs`) and **Export CSV…** button
  (`export_playlists`) snapshot the selections *and the `YTMusic` client*,
  lock the controls (`_start_work`), and start a daemon `threading.Thread`
  running `_worker` / `_export_worker` respectively.
- `_start_work` disables everything in `self.busy_controls` — Add, Export,
  **Log in and Refresh too** — and `_end_work` re-enables them. The last two
  matter because they race with a running job: logging in rebinds `self.yt`,
  which would switch accounts halfway through, and Refresh would drive the same
  client (and its `requests` session, which isn't thread-safe) from two threads.
  `login()` and `refresh_playlists()` also return early when `self.working`, so
  the invariant doesn't rest on widget state alone.
- The client is **passed to the worker as an argument**, never read off `self`
  mid-run, so a job stays bound to the account it started with.
- **Every** ytmusicapi call runs on a worker — including the two that used to be
  synchronous, connecting (`_connect` → `_connect_worker`) and refreshing
  (`refresh_playlists` → `_refresh_worker`). Both were noticeably blocking: a
  slow handshake or a library that paginates over many playlists made the window
  stop repainting and look hung. `_put_playlists(yt)` is the shared worker-side
  helper that fetches the library and queues it; `_add_to_playlists` ends with it
  too, so the post-add count refresh also happens off the UI thread (that
  replaced the old `("done", "refresh")` round trip).
- The workers do all network I/O and communicate *only* by putting
  `(kind, payload)` tuples on `self.worker_queue` (a `queue.Queue`). Kinds:
  `"log"` (a line for the Messages pane), `"step"` (advance progress bar),
  `"playlists"` (a fetched library → `_show_playlists`), `"account"` (text for
  the account label), `"connected"` (a live `YTMusic` client → `self.yt`),
  `"connect_failed"` (`(silent, message)`), and `"done"`.
- `_poll_worker`, rescheduled every 100 ms via `root.after`, drains the queue
  on the main thread and touches the widgets.
- Jobs with countable steps pass `_start_work(maximum=n)` and emit `"step"`;
  connect and refresh call `_start_work()` with no maximum, which puts the
  progress bar in `indeterminate` mode and animates it — on a slow network that
  animation is the difference between "working" and "hung". `_end_work` stops it
  and restores `determinate`.

Because the buttons only come back when a `"done"` message arrives, **both
worker entry points must always emit one.** `_worker` and `_export_worker` are
thin wrappers that call `_add_to_playlists` / `_export_to_csv` inside a
`try/except Exception` (logged as "Unexpected error…") with the `"done"` put in
a `finally` — a thread that dies on unforeseen data must not leave the UI
permanently disabled. `_poll_worker` reschedules itself in a `finally` for the
same reason: an exception escaping the drain loop would otherwise break the
`after` chain and silence every later worker.

**Rule: no Tk widget is ever touched from the worker thread.** Keep it that way
— violating it causes intermittent crashes that are miserable to reproduce.
No ytmusicapi call is left on the UI thread; if you add one, put it on a worker.

`self.working` guards against double-starting a job.

### The add operation — match, review, then add

Three stages, and the middle one is why the first two are separate jobs rather
than one loop. **No playlist may be touched while a decision is outstanding**, so
cancelling is predictable and the user can see the whole import before it changes
anything.

1. **Match phase** — `_worker` → `youtube.evaluate_songs()`. One
   `yt.search(f"{artist} {title}", filter="songs", limit=SEARCH_LIMIT)` per song
   (the station column is never part of the query), then `choose_match()`. Emits
   a log line per song and a `("step", …)` each. Per-song failures (search
   exception, unmatched) become `rejected` decisions and *don't* abort the run.
   This phase makes **no** mutating call. It finishes by handing
   `("decisions", (yt, evaluated, playlists))` to the UI.
2. **Review** — `_review_and_add()`, on the main thread. `_poll_worker` stashes
   the decisions and only runs this once that job's `("done", …)` has been
   processed, because `_end_work()` has to clear `self.working` before phase three
   can call `_start_work()` again. Each decision goes through
   `policy.action_for_match()`; `"ask"` results open `AmbiguousMatchDialog`
   sequentially. Ticking "use this choice for future ambiguous matches" calls
   `set_ambiguous_policy()`, which saves the setting, updates the dropdown, and
   therefore governs the *remaining* songs in the same run. Dismissing the dialog
   (Escape or the window close button) leaves `action` as `None`, which abandons
   the entire import — including high-confidence matches already approved.
3. **Add phase** — `_add_worker` → `youtube.add_video_ids_to_playlists()`. One
   `yt.add_playlist_items(playlistId, video_ids, duplicates=False)` call **per
   playlist** with all approved IDs batched — not one call per song, which would
   be slow and rate-limit-prone.

`duplicates=False` does **not** make YT Music skip songs already in the
playlist — it makes the whole batch fail atomically (nothing added) if even
one song is a duplicate, and ytmusicapi's `duplicates=True` would add the
duplicates. So on a failed status, `add_video_ids_to_playlists` fetches the
playlist's current videoIds, filters them out of the batch, and retries once with the rest
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

Distribution metadata is in `pyproject.toml` (setuptools backend, package
discovery via `[tool.setuptools.packages.find] include = ["cratefill*"]`, which
also keeps `tests/` out of the wheel). `ytmusicapi` is a hard dependency;
`tkinterdnd2` is the optional `[dnd]` extra and `pytest` the `[dev]` extra. The
console entry point is `[project.gui-scripts] cratefill = "cratefill.app:main"`
(`gui-scripts`, not `scripts`, so Windows launches it without a console window).

The version is declared **once**, as `__version__` in `cratefill/__init__.py`;
`pyproject.toml` marks it `dynamic` and reads it back through
`[tool.setuptools.dynamic] version = { attr = "cratefill.__version__" }`. That
attribute read is the reason `__init__.py` must not import the GUI — setuptools
has to get the version without pulling in Tkinter.

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
   tkinterdnd2 --collect-all ytmusicapi run_cratefill.py`. Build from
   `run_cratefill.py`, not from `cratefill/__main__.py`: bundlers execute a
   package's `__main__.py` as an ordinary script, which breaks its relative
   imports. Both `--collect-all` flags are essential:
   - `tkinterdnd2` bundles the native `tkdnd` binaries, without which the
     frozen app raises at `TkinterDnD.Tk()` in `main()` and won't start.
   - `ytmusicapi` bundles its `locales/` gettext `.mo` files. Without them
     the first ytmusicapi call in the frozen exe (e.g. `ytmusicapi.setup()`
     during login) dies with the misleading `[Errno 2] No translation file
     found for domain: 'base'` — masking whatever real error would have been
     raised (bad headers, missing cookie, etc.).

   This step is **not** in CI (it needs a Windows runner) — build locally
   and `gh release create` with the exe. The README's screenshot is a
   committed `docs/screenshot.png` referenced by absolute raw-GitHub URL so
   it renders on the PyPI project page too.

`build/`, `dist/`, and `*.spec` are gitignored.

## Testing

`py -m pytest` (pytest is the `[dev]` extra). The suite needs no Tk window, no
network and no `browser.json` — that independence is the main practical payoff of
the package split, so keep it.

- **`tests/test_matching.py`** — every stage directly: `normalize()` (casefolding,
  accents, ligatures, stylised names, punctuation), `split_featured()`,
  `version_markers()`/`has_version_conflict()` in both directions,
  `score_text()`, and `choose_match()` across a fixture set of harmless variants
  that must be accepted (accents, leading "The", featured artists, remasters,
  album versions), false positives that must be refused (`One`/`Someone`,
  `Cher`/`Cherub`, same title by another artist, right artist wrong song),
  recording variants that must be refused (live, remix, instrumental, cover,
  sped up, explicit), and the ambiguity margin. Plus a parametrised sweep of
  malformed ytmusicapi shapes (`artists=None`, missing keys, nameless artists,
  non-dict entries, `results=None`) — regression tests: a result with
  `artists=None` used to raise and take the worker thread down with it.
- **`tests/test_policy.py`** — the default and every fallback (absent,
  truncated, non-object, unknown value, unreadable), the save/load round trip,
  atomic writes leaving no partial file, unrelated keys surviving, and the
  `action_for_match` matrix — including that `high` ignores the policy and that
  `rejected` survives "Always add".
- **`tests/test_review.py`** — `CratefillApp._review_and_add` on a stub, with
  threading patched out: high-confidence added without prompting under every
  policy, rejected never added under any policy, skip/add policies applied
  without a prompt, remembering a choice governing the rest of the run, and a
  cancelled review mutating nothing at all.
- **`tests/test_dialog.py`** — `AmbiguousMatchDialog` itself: what it displays,
  Skip/Add, the remember checkbox, and that Escape or closing the window leaves
  `action` as `None` (which the caller reads as "cancel the import"). Skips
  automatically when there's no display.
- **`tests/test_storage.py`** — CSV encodings (BOM, cp1252), delimiters
  (`,` `;` tab), English/French headers, headerless files, station-by-header-name
  only, quoted fields; `read_songs_folder` including its deliberate
  non-recursiveness; `safe_filename`; `write_playlist_csv` collision suffixes and
  missing artists/album. `sample.csv` is resolved relative to the test file, not
  the working directory.
- **`tests/test_workers.py`** — a `FakeYT` double and a list for `put`, against
  `youtube.evaluate_songs` / `add_video_ids_to_playlists` /
  `export_playlists_to_csv` / `fetch_playlists`: search failure, no match,
  uncertain match, videoId dedup, the already-in-playlist retry, per-playlist
  failure isolation, step counts — and that the match phase issues no
  `add_playlist_items` at all. The completion guarantee is tested by calling the
  unbound wrappers on a stub holding only a `worker_queue`, so no Tk instance is
  needed.
- Headless UI construction is still the quick manual check after widget changes:
  `root = tk.Tk(); root.withdraw(); CratefillApp(root); root.update(); root.destroy()`.
- **Not** covered automatically: real search quality and real playlist mutation
  (needs a live session). Test those manually with `sample.csv` and a throwaway
  playlist. `_screenshot_preview.py` also needs a real X display — Pillow's
  `ImageGrab` cannot grab the root window under Wayland/XWayland.

## Known limitations / ideas for whoever takes over

- **No retry/rate-limit handling** on search. Fine for tens of songs; for
  hundreds, add a small delay or retry-on-exception in the search loop.
- **One modal per ambiguous song.** Fine for a handful, tedious for a long
  import. The intended replacement is a single batch-review table (Requested /
  Proposed / Confidence / Default action, high-confidence rows pre-checked,
  rejected rows disabled) confirmed in one go. The decision data is already in
  the right shape for it: `_review_and_add` receives every `MatchDecision`
  up-front, so only the UI needs replacing.
- **Matching ignores album and duration.** Cratefill already *exports* an Album
  column but doesn't read one on import, and a large duration difference is a
  strong signal of a live/extended/wrong recording. Both would improve ranking
  as secondary signals — album shouldn't be a hard requirement, since
  compilations and reissues rename it constantly.
- **Folder imports don't read audio tags.** `mutagen` would give real artist /
  title / album / duration instead of guessing from folder and file names.
- **No artist-ID cache.** YT Music search results carry artist IDs; caching the
  mapping after a high-confidence or user-confirmed match would let later songs
  by the same artist score more confidently. Seed it *only* from confirmed
  matches.
- **No undo.** Recording the items added by the last run would allow an "Undo
  last import" action — a safety net even after confirmation.
- **No matching report.** An optional CSV of requested/proposed/scores/action/
  reason would make threshold tuning far easier than reading the Messages pane.
- **Thresholds are guesses.** The constants at the top of `matching.py` were
  chosen conservatively and validated against a hand-written corpus, not real
  imports. `explicit`/`clean` in particular are treated as hard conflicts, which
  may prove too strict — ytmusicapi exposes an `isExplicit` field that the
  matcher currently ignores. Expect to revisit these with real data.
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
