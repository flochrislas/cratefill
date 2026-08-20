# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Cratefill is a small Tkinter desktop app (the `cratefill/` package) that loads songs from a CSV (artist + title) or from a folder of music files and adds them to the user's YouTube Music playlists via the unofficial [ytmusicapi](https://github.com/sigma67/ytmusicapi) library; it can also export playlists back to Artist/Title/Album CSV files. See `implementation-notes.md` for the full architecture walkthrough and design rationale; `RESEARCH.md` for why ytmusicapi was chosen over the official YouTube Data API.

## Commands

On this machine the bare `python` command is a broken Windows Store shim — **always use the `py` launcher**.

```powershell
py -m pip install -r requirements.txt   # ytmusicapi + tkinterdnd2 (optional, drag-and-drop)
py -m pip install -e ".[dev]"           # + pytest, for the test suite
py -m cratefill                         # run the app
py -m pytest                            # run the tests
```

There is no linter (packaging/release commands are under **Releasing**). Headless smoke test after UI changes:

```powershell
py -c "import tkinter as tk; from cratefill.app import CratefillApp; r = tk.Tk(); r.withdraw(); CratefillApp(r); r.update(); r.destroy(); print('OK')"
```

`tests/` covers `matching.py`, `policy.py` and `storage.py` directly, `youtube.py`'s worker functions and the review step against a fake client, and the review dialog itself (`test_dialog.py` skips without a display). No network and no `browser.json` needed. Add tests there rather than testing through the UI. Anything genuinely needing a live session (real search quality, real playlist mutation) still has to be checked manually.

## Releasing

Cratefill ships to **PyPI** (`pip install cratefill`) and as a **GitHub release** carrying a standalone Windows `.exe`. The version lives in **one** place: `__version__` in `cratefill/__init__.py`. `pyproject.toml` reads it via `[tool.setuptools.dynamic]`, which is why `__init__.py` must stay import-light — setuptools reads the attribute without executing the GUI.

1. Bump `__version__` in `cratefill/__init__.py`, commit, then tag and push:

   ```powershell
   git tag v0.2.0
   git push origin v0.2.0
   ```

   Pushing a `v*` tag triggers `.github/workflows/publish.yml`, which builds the sdist + wheel and publishes to PyPI over OIDC **trusted publishing** — no token is stored. (The one-time PyPI publisher config is already done: owner `flochrislas`, repo `cratefill`, workflow `publish.yml`, environment `pypi`.) Watch the run under the repo's Actions tab.

2. The Windows `.exe` is **not** built by CI (it needs a Windows runner) — build and attach it manually:

   ```powershell
   py -m PyInstaller --onefile --windowed --name Cratefill --collect-all tkinterdnd2 --collect-all ytmusicapi run_cratefill.py
   gh release create v0.2.0 dist/Cratefill.exe --title "Cratefill v0.2.0" --notes "..."
   ```

   Build from `run_cratefill.py`, **not** from `cratefill/__main__.py`: bundlers run `__main__.py` as a plain script, which breaks its relative imports.

   Both `--collect-all` flags are required:
   - `tkinterdnd2` bundles the native tkdnd binaries — without it the frozen app crashes at `TkinterDnD.Tk()`.
   - `ytmusicapi` bundles its `locales/*.mo` gettext files — without them the first ytmusicapi call in the exe (typically `ytmusicapi.setup()` during login) dies with the misleading `[Errno 2] No translation file found for domain: 'base'`, masking the real error.

   `rapidfuzz` ships compiled extension modules. PyInstaller normally picks those up on its own, but **this has not been verified on Windows yet** — if the frozen app fails to import `rapidfuzz`, add `--collect-all rapidfuzz` as well. Check this before the next release.

To inspect the dists before tagging: `py -m build` then `py -m twine check dist/*`. Manual PyPI upload fallback (needs a `pypi-…` token and an **interactive** terminal — it can't be backgrounded, twine prompts for the token): `py -m twine upload dist/cratefill-<ver>*`. Build artifacts (`build/`, `dist/`, `*.spec`) are gitignored.

## Architecture essentials

- **Module boundaries** (`cratefill/`): `app.py` owns Tk — window, theme, dialogs, threads, queue polling. `matching.py` is pure matching (only `re`, `unicodedata`, `rapidfuzz`). `policy.py` is what an ambiguous match *means* plus settings persistence. `storage.py` is local files (CSV, folders, filenames, the per-user data dir, atomic JSON); no Tk, no ytmusicapi. `youtube.py` owns credentials and every network call; no Tk. `__init__.py` holds only `__version__` and must stay import-light. Dependencies run one way: `__main__ → app → {youtube → {matching, storage}, policy → storage}`. Respect these: a Tk import outside `app.py`, a `yt.*` call in `app.py`, or `matching.py` learning about policy is a regression. Don't add `dialogs.py`/`models.py`/`workers.py`/`utils.py` until a boundary actually demands it.
- **Matching is allowed to refuse.** `matching.choose_match` returns a `MatchDecision` with status `high` / `ambiguous` / `rejected`. There is **no first-result fallback** — if nothing clears the minimums the answer is `rejected`. Artist and title are thresholded *independently* (`MIN_TITLE`/`MIN_ARTIST`), so a perfect title can never carry a wrong artist. Comparison is on whole normalized tokens, never substrings (that is what made `one → someone` and `cher → cherub` match). Hard version markers (live, remix, karaoke, instrumental, cover, sped up, explicit…) must agree in **both** directions; soft ones (remastered, deluxe, album version…) are stripped by `core_title`. Thresholds are module constants at the top of `matching.py` — tune there, with tests.
- **Policy never overrides safety.** `policy.action_for_match` returns `add` for `high` and `skip` for `rejected` whatever the saved setting says; the **On ambiguous match** dropdown (`ask`/`skip`/`add`, default `ask`) only governs the ambiguous middle. "Always add" must never rescue a failed threshold or a version conflict. The setting lives in `settings.json` in the per-user data dir — **not** in `browser.json`, which is for credentials only.
- **Adding is two phases, and phase one mutates nothing.** `_worker` → `youtube.evaluate_songs` (search + score, emits `("decisions", …)`), then the review happens on the main thread in `_review_and_add`, then `_add_worker` → `youtube.add_video_ids_to_playlists`. Dismissing the review dialog cancels the whole import, including already-approved high-confidence matches. Keep it that way: no playlist may be touched while a decision is outstanding.
- **Threading rule:** network I/O runs in worker threads (`_worker` for adding, `_export_worker` for export, `_connect_worker`, `_refresh_worker`) that communicate only via `self.worker_queue`, drained on the main thread by `_poll_worker` (`root.after` loop). Never touch a Tk widget from a worker thread. **No ytmusicapi call may run on the UI thread**; login validation is threaded too (`LoginDialog._validate_worker` has its own `result_queue` + `_poll_validation`). The `app.py` workers are thin wrappers: the actual work is `youtube.evaluate_songs` / `add_video_ids_to_playlists` / `export_playlists_to_csv` / `fetch_playlists` / `open_session` / `save_credentials`, which take a `put` callable so they stay Tk-free. Queue kinds: `"log"`, `"step"`, `"playlists"`, `"account"`, `"connected"`, `"connect_failed"`, `"decisions"`, `"done"`. `_start_work()` with no `maximum` animates the progress bar (indeterminate) for jobs with no countable steps.
- `"decisions"` is stashed by `_poll_worker`, not acted on immediately: the review must wait for that job's `"done"` so `_end_work()` has cleared `self.working` before phase two calls `_start_work` again.
- **No YouTube Music call may race a running job.** `_start_work`/`_end_work` disable and re-enable all of `self.busy_controls` (Add, Export, **Log in, Refresh**), and `login`/`refresh_playlists` re-check `self.working`. The `YTMusic` client is passed into the worker as an argument — never read off `self.yt` inside one, or a mid-run login switches accounts halfway through. Add any new API-calling control to `busy_controls`.
- Selection mapping: song Treeview row iids are string indices into `self.songs`; playlist Listbox indices map into `self.playlists`. Preserve these mappings if adding sorting/filtering — column-click sorting (`sort_songs`) already does it right by only reordering rows with `tree.move`, never changing iids.
- Songs are `(artist, title, station)` tuples. The optional station column ("where I heard this") is display-only context: hidden via `displaycolumns` when the CSV has none, and never part of the YouTube Music search query.
- Auth is ytmusicapi browser auth: pasted request headers → `browser.json` in the per-user data dir from `user_data_dir()` (`%APPDATA%\Cratefill`, `~/Library/Application Support/Cratefill`, or `$XDG_CONFIG_HOME/cratefill`), **not** next to the script. Always write it through `secure_auth_dir()`/`secure_auth_file()` — ytmusicapi creates the file with the process umask, so the `0600` chmod has to be applied afterwards. `LoginDialog.submit` must keep staging into a `mkstemp` sibling and `os.replace`-ing only after validation succeeds: never point `ytmusicapi.setup` at the live `browser.json`, or a mistyped re-login destroys a working session. `migrate_legacy_auth_file()` relocates a file left by earlier versions in the app directory on startup. **`browser.json` holds the user's session cookies — never commit it** (it's in `.gitignore`).
- Searches are one call per song; playlist adds are batched (one `add_playlist_items` call per playlist with all videoIds), with `duplicates=False`. **YT Music fails such a batch atomically if even one song is already in the playlist** (and `duplicates=True` would add the duplicates), so on failure `youtube.add_video_ids_to_playlists` fetches the playlist, filters out already-present videoIds, and retries once with the rest.

## Gotchas

- ytmusicapi is unofficial and tracks YT Music's private web API. If YT-side calls suddenly break, first try `py -m pip install -U ytmusicapi` and check the library's GitHub issues.
- CSV input is messy by design: the parser handles UTF-8/cp1252, comma/semicolon/tab, English+French header names (`ARTIST_HEADERS`/`TITLE_HEADERS`/`STATION_HEADERS` tuples), and headerless files. Don't simplify this away; extend the header tuples to support new column names. The station column is matched by header name only, never positionally.
- `exportselection=False` on the playlist Listbox is required — without it, clicking the song pane clears the playlist selection.
- Treat every ytmusicapi field as optional: results can have `videoId=None` (`choose_match` filters these), `artists` can be missing/`null`/hold nameless entries, `title` can be absent. Use `x.get("artists") or []`, never `x.get("artists", [])` — the latter returns `None` when the key exists with a null value and then raises.
- **Workers must always emit `("done", …)`.** `_worker`/`_add_worker`/`_export_worker`/`_connect_worker`/`_refresh_worker` are wrappers whose `finally` puts it; the real work lives in `youtube.py`. Without that, one unexpected exception leaves Add/Export disabled until restart. `_poll_worker` reschedules itself in a `finally` for the same reason. In `_add_worker` the playlist refetch is nested inside its own `try/finally` so it can't preempt the `done`.
- The UI is dark-only and must stay cross-platform (Windows + Linux): the theme is hand-rolled in `apply_dark_theme()` on top of the built-in `clam` theme — do not switch to platform themes (`vista`, `winnative`) or to sv-ttk (tried; it applies empty styles on this Python 3.14/Tk 8.6.15 build). When adding plain tk widgets (Text, Listbox), style them with `DARK_TEXT_STYLE`/`DARK_LIST_STYLE`; Listbox rejects `insertbackground` (that's why two dicts exist).
- `_screenshot_preview.py` renders the app with fake data and saves `ui_preview.png` (requires `pillow`) — use it to verify UI changes visually.
- Drag-and-drop needs `tkinterdnd2` *and* a `TkinterDnD.Tk()` root (created in `main()`). Both are optional everywhere else: the import is guarded, and `_build_ui` swallows the `TclError` from `drop_target_register` when the root is a plain `tk.Tk()` (tests, previews). Keep new code working without tkinterdnd2 installed.
