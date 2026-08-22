# Cratefill — Implementation Notes

Notes for a developer taking over the project. Read `README.md` first for what
the app does from a user's point of view; this file explains how it's built and
why. `RESEARCH.md` documents the alternatives that were considered before
settling on this approach.

*Last updated: 2026-08-21 — matches the `cratefill/` package as of that date (v0.1.1).*

## Stack and key decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3 (developed on 3.14) | ytmusicapi is a Python library |
| YouTube Music access | [ytmusicapi](https://github.com/sigma67/ytmusicapi) (unofficial) | No API quota; searches the actual YT Music song catalog. The official YouTube Data API v3 costs ~150 quota units per track (≈65 tracks/day on the default 10k quota) and searches all of YouTube, not just music — see `RESEARCH.md` |
| GUI | Tkinter (`ttk` widgets) | Ships with Python — no packaging issues on Windows |
| Theme | Hand-rolled dark theme on built-in `clam` (`apply_dark_theme()`) | `clam` is the one built-in ttk theme that renders identically on Windows and Linux, so the dark UI is cross-platform with zero dependencies. sv-ttk was tried first and abandoned: on this Python 3.14 / Tk 8.6.15 build it registered its theme name but applied empty style settings (half-light UI). Palette lives in module constants (`BG`, `FIELD`, `BTN`, `FG`, `ACCENT`…); plain tk widgets (Text, Listbox) aren't covered by ttk themes and take `DARK_LIST_STYLE`/`DARK_TEXT_STYLE` directly. The title bar is darkened via `enable_dark_title_bar()` (Windows DWM attribute, best-effort no-op elsewhere; Linux title bars follow the desktop's window manager theme) |
| Architecture | Small package, `cratefill/` (`app`, `matching`, `policy`, `storage`, `youtube`) | Started as one file; split at ~1100 lines because UI layout, matching rules, local file handling and YouTube Music communication change for unrelated reasons, and the improved matching system needs a pure, testable core. Kept deliberately modest — no `utils.py`, no module-per-class |
| Song matching | Token-aware scoring on top of `rapidfuzz`, four confidence tiers, version-marker checks | Substring matching accepted `one → someone` and `cher → cherub`, and the old first-result fallback added unrelated songs silently. `rapidfuzz` gives order-tolerant scorers far better than `difflib` and is fast enough to be irrelevant at these sizes; the fuzzy call is isolated in `_ratio()` so it can be swapped. Use `token_sort_ratio`, **not** `token_set_ratio`/`WRatio`: those deduplicate tokens, which made `Run Run Run` and `Run` score identically |
| Ambiguous matches | User policy in `settings.json` (`ask`/`skip`/`add`, default `ask`), applied by `policy.py`; the `weak` tier ignores it | Matching can only say how credible a candidate is; what to do about "credible but uncertain" is a user preference — but the *thinnest* evidence is always reviewed, because "Always add" shouldn't accept what a user would reject on sight. Kept out of `matching.py` so the rules stay pure and testable, and out of `browser.json` so a preference never shares a file with session cookies |
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
├── matching.py    normalize/tokens, metadata_segments/core_title/score_title,
│                  version_markers/version_relation, score_text/score_artist,
│                  has_content_overlap, Candidate,
│                  choose_match → MatchDecision (high/ambiguous/weak/rejected)
│                  pure: re + unicodedata + collections + rapidfuzz, no I/O
├── policy.py      POLICIES ("ask"/"skip"/"add"), load_policy/save_policy,
│                  migrate_settings(), SETTINGS_VERSION,
│                  action_for_match(decision, policy) → "add"/"skip"/"ask"
├── selftest.py    run() → does this build have everything? (--selftest)
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
                   class AmbiguousMatchDialog(Toplevel)  pick a candidate, Skip/Add
                   class CratefillApp           window, selections, threads, queue
                   main()

run_cratefill.py   PyInstaller entry script (see PyInstaller section)
tests/             test_matching.py, test_policy.py, test_selftest.py,
                   test_storage.py, test_workers.py, test_review.py,
                   test_dialog.py (needs a display)
```

Dependency direction is one-way:
`__main__ → app → {youtube → {matching, storage}, policy → storage}`. Nothing
imports `app`. `matching.py` imports only `re`, `unicodedata`, `collections` and `rapidfuzz`;
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
| `high` | near-identical artist and title, the recording asked for, clear winner | added |
| `ambiguous` | recognisably the same song, but something is off | the user's policy decides |
| `weak` | plausibly the same song, on thin evidence | **always asks**, whatever the policy |
| `rejected` | a *different* song — no result shared a content word in the title | always skipped |

`weak` exists because "offer it and let the user glance at it" is only true when
the user is actually asked. A loose title overlap (`Hello` → `Hello World
Goodbye`) or a wholly different performer is too thin to hand to a saved
"Always add", so `_classify()` pins it to the ask path via
`WEAK_TITLE` / `WEAK_ARTIST`.

**Two opposite failures are being avoided here, and the balance between them is
the whole design.**

The original heuristic compared substrings and, when nothing matched, returned
the first search result anyway — flagged `?` but added regardless. That made
`Cher — One` silently become `Cherub — Someone` and turned studio requests into
live takes and karaoke tracks. So substring comparison went, and confidence
became hard to earn.

But the first replacement over-corrected: it required artist *and* title to clear
minimums and refused any version mismatch outright, so a live-only track, a
remix, or another band's cover produced **nothing at all**. Coming back
empty-handed is the worse failure — a proposal the user can glance at and accept
beats a silent miss. So:

* **`rejected` is reserved for a different song.** The bar is a shared *content*
  word in the title — `has_content_overlap()` discounts `STOP_WORDS`, so
  `The End` and `The Beginning` don't qualify on "the". That still rejects
  `One → Someone`, `Cher → Cherub`, and `Lisztomania → 1901`.
* **A wrong artist or wrong recording costs points and blocks `high`** — it never
  excludes. Every shortfall is named in `reasons`, so an offer is explicit rather
  than silent. This is the sense in which a high title score still can't
  compensate for the wrong artist: it can't make the match *confident*.
* **`high` is deliberately hard to earn**, so everything doubtful reaches the
  user rather than being assumed — and the thinnest evidence is pinned to `weak`,
  which reaches the user even under "Always add".

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
4. **Keep version information — but only where it lives.** Hard markers are live,
   remix, acoustic, instrumental, karaoke, cover, demo, radio edit, extended,
   sped up, slowed, clean, explicit. `version_markers()` looks for them **only in
   metadata positions** (`metadata_segments()`: bracketed groups, and a trailing
   `- …` segment — YouTube Music uses both forms). Scanning the whole title
   instead was a real bug: songs actually called *Clean*, *Stereo* and *Live and
   Let Die* were reduced to empty strings and then **rejected as no match**. The
   cost of the fix is that a request typed as `Wonderwall Live`, with no brackets
   or dash, no longer reads as asking for the live take.

   `core_title()` strips those segments before scoring, dropping a bracketed
   group **whole** rather than word by word, because `(Live at Wembley)` is one
   piece of metadata — keeping `at wembley` made the live take look like a
   different song and dragged it below an unrelated band's studio cut. Soft
   markers (remastered, deluxe, anniversary, album version, official video…) and
   `feat. X` go the same way, which is why `Wonderwall` scores 1.00 against both
   `Wonderwall (Remastered)` and `Wonderwall (Live at Wembley)`.

   Two backstops guard the identity of the song: `core_title()` **never returns
   `""`** for a non-empty title (if the metadata was the title, the title wins),
   and `score_title()` short-circuits to 1.0 on exact normalized equality before
   any stripping happens. `TestExactMatchInvariant` runs every marker word as a
   whole title to keep it that way.

   `version_relation()` then compares hard markers **asymmetrically**, because
   the two directions aren't equally disappointing:

   | relation | example | penalty |
   |---|---|---|
   | `same` | ask *Wonderwall (Live)*, get *Wonderwall (Live)* | 0 — can be `high` |
   | `missing` | ask *Wonderwall (Live)*, get *Wonderwall* | 0.10 |
   | `extra` | ask *Wonderwall*, get *Wonderwall (Live)* | 0.20 |

   `VERSION_PENALTY` is applied to the score, **not** used to exclude. That is
   deliberate and was got wrong once: filtering on version meant a tribute band's
   exact studio version outranked the real artist's live take. Scaling the score
   instead lets being the right performer outweigh being the right recording,
   while still preferring the exact version when it exists.
5. **Compare whole tokens, counting repeats** — `score_text()` scores 1.0 for
   equal token **multisets** (`collections.Counter`): order is irrelevant,
   repetition is not, because comparing *sets* made `Run Run Run` and `Run`
   identical. Otherwise the tokens must genuinely intersect before any fuzzy
   score is trusted — the gate that kills `one → someone` and `cher → cherub`,
   where character similarity is high but no whole word is shared. Overlap is
   divided by the **longer** side, so `hello` can't pass as `hello world
   goodbye`. Values of four characters or fewer get no fuzzy credit, but only
   when *both* sides are a single word. The fuzzy part is
   `max(token_sort_ratio, ratio) / 100` in `_ratio()` — **not** `token_set_ratio`
   or `WRatio`, both of which deduplicate tokens and reintroduce the `Run Run
   Run` bug.

   `_score_spaceless()` is the fallback for scripts that don't separate words
   (CJK, Thai): there the whole title is one token, so any spelling variation
   shares nothing and the gate above can never fire. It applies only when both
   sides are single-token and at least one is in such a script, with a high
   `SPACELESS_MIN` cutoff — script-gated precisely so it can't revive substring
   matching for Latin titles.
6. **Score the artist** — `score_artist()` returns `(principal, combined)`:
   `principal` is how well the requested principal artist is represented,
   `combined` adds `GUEST_BONUS` per matched guest (capped at 1.0). **Only
   `principal` may decide confidence**; `combined` is for ranking. Both halves of
   that were bugs. First, a plain `max()` over principal and guests let a perfect
   featured-artist match hide a completely absent principal — `Jay-Z feat. Alicia
   Keys` matched a result credited to Alicia Keys alone at 1.00 and went straight
   to `high` (now ~0.05). Then, comparing the *combined* score against
   `HIGH_ARTIST` let one matching guest lift a near-miss principal over the bar:
   `Nick Cave and the Bad Seeds` vs `Nick Cave & The Bad Seeds` scores 0.833, plus
   0.05 made 0.883, clearing the 0.88 gate the principal itself had failed.
   `Candidate.principal_score` exists so `_shortfalls()` and `_classify()` test
   the right number. A leading "the" is ignored (`The Beatles ≡ Beatles`), nameless
   and malformed entries are skipped, and extra artists on the result never
   penalise it. `with` counts as a featured separator **here only** — in a title
   it's an ordinary word.
7. **Rank and classify** — every candidate becomes a `Candidate` carrying its own
   scores, `relation` and `reasons`, ranked by `base × (1 − version penalty)`
   where `base = 0.65·title + 0.35·artist`. Only candidates with no shared
   content word are dropped. `high` needs `HIGH_TITLE` / `HIGH_ARTIST`,
   `relation == "same"`, *and* a `WINNER_MARGIN` lead over the runner-up.
   Otherwise `_classify()` returns `weak` when the winner is below `WEAK_TITLE`
   or `WEAK_ARTIST`, else `ambiguous` — with `reasons` naming each shortfall,
   including which artist was actually found.

   `MatchDecision.alternatives` holds the runners-up as `Candidate`s, and
   `.choices` is the winner followed by them — that's what the review dialog
   renders as a radio list. Keeping the scores and reasons on the candidate is
   what lets the UI list rivals without recomputing anything.

All thresholds are module constants at the top of the file. They encode one
trade-off: being *confident* is hard to earn, but being *offered* is easy — a
prompt costs the user a click, a silent miss costs them the song. Loosen them
only with real examples and tests.

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
weak     → "ask"    whatever the setting says
ambiguous→ the saved policy ("ask" | "skip" | "add")
```

The asymmetry is the safety property: **"Always add" means "accept credible but
uncertain candidates", never "add the first unrelated search result"** — and
never "accept something I'd have rejected on sight", which is what `weak` is for.

Three things guard that setting, because it is the one place where the permissive
matcher is not reviewed by a human:

* Selecting **Always add** raises a confirmation spelling out what it now accepts
  (a different recording, a different artist); cancelling reverts the dropdown.
* `weak` decisions ignore it entirely.
* `migrate_settings()` resets a saved `add` to `ask` when `SETTINGS_VERSION`
  moves. **Bump `SETTINGS_VERSION` whenever a release changes what `ambiguous`
  covers** — a standing instruction to skip review should only ever apply to
  rules the user actually agreed to. `CratefillApp.__init__` calls it beside
  `migrate_legacy_auth_file()` and logs a line when it fires.

  It returns `(reset, saved)`, and the two can disagree: on a read-only config
  directory the reset is still *required*, it just can't be persisted. The caller
  must apply `reset` to its in-memory policy regardless of `saved` — hence
  `set_ambiguous_policy(policy.ASK, persist=saved)`. Returning only "a reset
  happened" was a bug: the app announced the reset, then took the new value from
  `load_policy()`, which re-read the unchanged file and handed back `add`. It
  logged that it would ask and went on adding silently for the whole session.

The setting is stored as `{"ambiguous_match_policy": "ask", "settings_version": 2}`
in `settings.json` in `storage.user_data_dir()` — beside `browser.json`, never
inside it: a preference has no business sharing a file with session cookies.
Writes go through `storage.write_json_atomic()` (staged sibling + `os.replace`,
same reasoning as `save_credentials`) and preserve unrelated keys, so a future
version's settings survive an older version writing. Anything wrong with the
file — absent, unreadable, malformed, not an object, unknown value — falls back
to `ask`.

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
   sequentially. The dialog shows the request, the decision-level reason, and
   `decision.choices` as a **radio list** — the proposal plus its near-scoring
   rivals, each with its own score and shortfall. Whichever is selected becomes
   `dialog.chosen`, and that is the videoId `_review_and_add` approves: the
   top-ranked candidate is not always the one the user wants, which is exactly
   what "another candidate scores almost the same" is telling them. Ticking "use
   this choice for future ambiguous matches" calls `set_ambiguous_policy()`,
   which saves the setting, updates the dropdown, and therefore governs the
   *remaining* songs in the same run (the checkbox is hidden for `weak`
   decisions, which can't be automated anyway). Dismissing the dialog (Escape or
   the window close button) leaves `action` as `None`, which abandons the entire
   import — including high-confidence matches already approved.
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
the package split, so keep it. `tests/test_dialog.py` is the exception and skips
itself when there is no display.

Separately, **`py -m cratefill --selftest`** answers a different question: not
"is the logic right" but "does *this build* have everything it needs". It exists
for the frozen exe, where both known failure modes (a missing `rapidfuzz`, missing
ytmusicapi gettext catalogues) happen at import time and a `--windowed` build
shows nothing at all — it just doesn't open. Exit code 0/1, so it works from a
script or CI. See `cratefill/selftest.py` and the **Releasing** section.

- **`tests/test_matching.py`** — every stage directly: `normalize()` (casefolding,
  accents, ligatures, stylised names, punctuation), `split_featured()`,
  `version_markers()`/`version_relation()` in both directions, `core_title()`,
  `score_text()`, and `choose_match()` across harmless variants that must be
  accepted (accents, leading "The", featured artists, remasters, album versions),
  different songs that must be refused (`One`/`Someone`, `Cher`/`Cherub`, right
  artist wrong song), and recording variants that must be *offered* rather than
  refused. Plus a parametrised sweep of malformed ytmusicapi shapes
  (`artists=None`, missing keys, nameless artists, non-dict entries,
  `results=None`) — a result with `artists=None` used to raise and take the worker
  thread down with it.

  Four classes exist because a review found real bugs in exactly those places,
  and they are the regression net for them: `TestExactMatchInvariant` (an exact
  artist+title result must always be `high` — every marker word is tried as a
  whole title, plus titles containing "with"), `TestPrincipalArtist` (a guest can
  never stand in for the principal), `TestRepeatedWords` (`Run Run Run` ≠ `Run`),
  and `TestStopWordFloor` / `TestWeakTier` / `TestSpacelessScripts`.
- **`tests/test_selftest.py`** — the build self-check. Mostly proves it *fails*:
  a self-test that always passes is worse than none, so each required check is
  broken in turn and the run must go non-zero, with the two real frozen-build
  failures (missing `rapidfuzz`, missing ytmusicapi locales) named explicitly.
  Also that an absent `tkinterdnd2` only warns and a missing display only skips.
- **`tests/test_policy.py`** — the default and every fallback (absent,
  truncated, non-object, unknown value, unreadable), the save/load round trip,
  atomic writes leaving no partial file, unrelated keys surviving,
  `migrate_settings()` (a stale `add` becomes `ask`, exactly once), and the
  `action_for_match` matrix — including that `high` ignores the policy, that
  `rejected` survives "Always add", and that `weak` always asks.
- **`tests/test_review.py`** — `CratefillApp._review_and_add` on a stub, with
  threading patched out: high-confidence added without prompting under every
  policy, rejected never added under any policy, skip/add policies applied
  without a prompt, remembering a choice governing the rest of the run, weak
  matches prompting even with `add` saved, picking an alternative changing what
  gets added, and a cancelled review mutating nothing at all.
- **`tests/test_dialog.py`** — `AmbiguousMatchDialog` itself: what it displays,
  the alternatives radio list (every candidate listed with its own shortfall,
  the winner selected by default, picking another changing `chosen`), Skip/Add,
  the remember checkbox, the weak-match caption and its hidden checkbox, and
  that Escape or closing the window leaves `action` as `None` (which the caller
  reads as "cancel the import"). Skips automatically when there's no display.
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
  validated against a hand-written corpus, not real imports — a code review
  found several false-confidence cases that all 239 tests of the day missed, so
  treat the corpus as a floor, not proof. Things to watch: `explicit`/`clean` are
  treated as version markers even though ytmusicapi exposes a separate
  `isExplicit` field the matcher ignores, so an explicit-tagged track may prompt
  when it shouldn't; version markers are only read from brackets or a trailing
  `- …`, so `Wonderwall Live` typed without punctuation reads as a plain title;
  and a loose title overlap reaches the user as a `weak` proposal rather than
  being dropped. All deliberate — a prompt beats a silent miss — but worth
  revisiting with real data. **A matching-report CSV is the tool that would
  actually calibrate these**, which is why it stays high on the list below.
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
