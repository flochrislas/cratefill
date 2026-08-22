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
py -m cratefill --selftest              # check the install/bundle, no GUI (exit 0 = fine)
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

2. The Windows `.exe` is **not** built by CI (it needs a Windows runner) — build and attach it manually.

   **Build a console version first and run its self-check.** `--windowed` sends startup errors nowhere, and every way a bundle breaks here is an *import-time* failure, so a `--windowed` exe with a missing module simply fails to open with no message at all:

   ```powershell
   py -m PyInstaller --onefile --name CratefillTest --collect-all tkinterdnd2 --collect-all ytmusicapi run_cratefill.py
   dist\CratefillTest.exe --selftest        # exit 0 = the bundle has everything
   ```

   `--selftest` (see `cratefill/selftest.py`) checks rapidfuzz actually *scores*, the matching pipeline classifies both ways, the per-user data dir is writable, the settings file is readable, and ytmusicapi loads its gettext catalogues — no GUI and no login. Then build the real thing:

   ```powershell
   py -m PyInstaller --onefile --windowed --name Cratefill --collect-all tkinterdnd2 --collect-all ytmusicapi run_cratefill.py
   gh release create v0.2.0 dist/Cratefill.exe --title "Cratefill v0.2.0" --notes "..."
   ```

   Build from `run_cratefill.py`, **not** from `cratefill/__main__.py`: bundlers run `__main__.py` as a plain script, which breaks its relative imports.

   Both `--collect-all` flags are required:
   - `tkinterdnd2` bundles the native tkdnd binaries — without it the frozen app crashes at `TkinterDnD.Tk()`. The self-check reports its absence as a warning, not a failure, since the app runs without it.
   - `ytmusicapi` bundles its `locales/*.mo` gettext files — without them the first ytmusicapi call in the exe (typically `ytmusicapi.setup()` during login) dies with the misleading `[Errno 2] No translation file found for domain: 'base'`, masking the real error. `--selftest` triggers that path deliberately via `YTMusic(language="en")`, which needs no auth and no network.

   `rapidfuzz` ships compiled extension modules, loaded at *import* time. PyInstaller normally picks those up on its own, but **this has not been verified on Windows yet** — if `--selftest` reports `rapidfuzz`, add `--collect-all rapidfuzz` and update this command.

   Also worth confirming on Windows, since the paths are platform-specific and have only ever run on Linux: `%APPDATA%\Cratefill\browser.json` after logging in, `settings.json` beside it after changing the dropdown, and that a `browser.json` left next to the exe gets migrated on launch.

To inspect the dists before tagging: `py -m build` then `py -m twine check dist/*`. Manual PyPI upload fallback (needs a `pypi-…` token and an **interactive** terminal — it can't be backgrounded, twine prompts for the token): `py -m twine upload dist/cratefill-<ver>*`. Build artifacts (`build/`, `dist/`, `*.spec`) are gitignored.

## Architecture essentials

- **Module boundaries** (`cratefill/`): `app.py` owns Tk — window, theme, dialogs, threads, queue polling. `matching.py` is pure matching (only `re`, `unicodedata`, `collections`, `rapidfuzz`). `policy.py` is what an ambiguous match *means* plus settings persistence. `selftest.py` verifies a build has its dependencies (the one place allowed to import Tk outside `app.py`, and only inside a function). `storage.py` is local files (CSV, folders, filenames, the per-user data dir, atomic JSON); no Tk, no ytmusicapi. `youtube.py` owns credentials and every network call; no Tk. `__init__.py` holds only `__version__` and must stay import-light. Dependencies run one way: `__main__ → app → {youtube → {matching, storage}, policy → storage}`. Respect these: a Tk import outside `app.py`, a `yt.*` call in `app.py`, or `matching.py` learning about policy is a regression. Don't add `dialogs.py`/`models.py`/`workers.py`/`utils.py` until a boundary actually demands it.
- **Coming back empty-handed is the worst outcome.** `matching.choose_match` returns a `MatchDecision` with status `high` / `ambiguous` / `weak` / `rejected`. `rejected` means **a different song**: no result shared a *content* word with the requested title (stop words don't count — see `STOP_WORDS`/`has_content_overlap`). A remix, a live take, a karaoke version or another band's cover of the right song is *offered*, because a reviewable proposal beats a silent miss. There is still no first-result fallback: `one → someone` and `cher → cherub` share no whole word and stay rejected, and so does another track by the right artist.
- **A wrong artist or wrong version costs points, it doesn't exclude.** Both block `high` and are named in `reasons`; the version difference is folded into the score through `VERSION_PENALTY` (`same` 0, `missing` 0.10, `extra` 0.20) rather than filtering. That ranking matters: the real artist's live take must beat a tribute band's studio cut, which a hard version filter got backwards. `version_relation` is asymmetric — `extra` (a marker you didn't ask for) is penalised harder than `missing` (only the standard recording exists).
- **The principal artist carries the requirement.** `score_artist` returns `(principal, combined)` and they are **not** interchangeable: `principal` alone decides whether `high` is allowed, `combined` (principal + `GUEST_BONUS` per matched guest) is for ranking only. Collapsing them let a guest bonus lift a 0.833 principal to 0.883 and clear the 0.88 gate — `Candidate.principal_score` is what `_shortfalls`/`_classify` test. A perfect featured-artist match also can't stand in for a missing principal (`Jay-Z feat. Alicia Keys` credited to Alicia Keys alone scores ~0.05). `with` is a separator in *artist* names only — in a title it's an ordinary word, and splitting on it reduced `With or Without You` to nothing.
- **Only strip metadata from metadata positions.** `version_markers`/`core_title` look at bracketed groups and a trailing `- …` segment, never the whole title: songs called `Clean`, `Stereo` and `Live and Let Die` were being erased and then rejected. A bracketed group naming a version is dropped *whole* (`Wonderwall (Live at Wembley)` still scores 1.00), `core_title` never returns `""` for a non-empty title, and `score_title` short-circuits on exact normalized equality before any stripping. `tests/test_matching.py::TestExactMatchInvariant` runs every marker word as a whole title — **an exact artist+title result must always be `high`.**
- Comparison is on whole normalized tokens, never substrings, and on token *multisets* so `Run Run Run` ≠ `Run` (hence `token_sort_ratio`, not `token_set_ratio`). `_score_spaceless` adds a character-similarity fallback for scripts without word boundaries (CJK, Thai), script-gated so it can't revive substring matching for Latin titles. Thresholds are module constants at the top of `matching.py` — tune there, with tests.
- **Four tiers, and the policy only governs one.** `policy.action_for_match` returns `add` for `high` and `skip` for `rejected` whatever the saved setting says, and **`ask` for `weak` whatever the setting says** — a thin match (loose title overlap, or a wholly different performer) must never be automated, because "the user can glance at the proposal" only holds if they're asked. The **On ambiguous match** dropdown (`ask`/`skip`/`add`, default `ask`) governs `ambiguous` alone. Switching it to `add` warns first, and `policy.migrate_settings()` resets a stale `add` to `ask` when `SETTINGS_VERSION` moves — bump it whenever a release changes what `ambiguous` covers. It returns `(reset, saved)`: **apply `reset` to the in-memory policy even when `saved` is False**, and never re-read the file to find the new value. On a read-only config dir the reset is required but unpersistable, and reporting only "reset happened" made the app announce the reset while `load_policy()` still returned `add` — it kept adding unreviewed all session. The setting lives in `settings.json` in the per-user data dir — **not** in `browser.json`, which is for credentials only.
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
- **To screenshot the UI, run it under Xvfb, not the real desktop.** On a Wayland session, `import`/`xwd` grabs of XWayland windows work only intermittently and eventually fail with `BadMatch (X_GetImage)` or report no windows at all, with the app still running. A private X server has no compositor in the way and is fully scriptable:

  ```bash
  export DISPLAY=:77 && Xvfb :77 -screen 0 1200x800x24 & sleep 1.5
  .venv/bin/python -m cratefill & sleep 4
  import -window "$(xdotool search --name '^Cratefill' | tail -1)" /tmp/shot.png
  ```

  With no window manager, avoid `wait_visibility()` (it blocks forever) and call `grab_release()` on modal dialogs; keep the root window mapped rather than `withdraw()`n, then `update()` a few times before grabbing. Pillow's `ImageGrab` needs a real X display and can't grab the root window under Wayland at all.
- ttk indicator styling differs by theme: clam's `Radiobutton.indicator`/`Checkbutton.indicator` take **`indicatorbackground`/`indicatorforeground`**, not the default theme's `indicatorcolor`. Setting the wrong one is silently ignored and leaves a light circle that reads as *selected* when it isn't. Check with `style.element_options("Radiobutton.indicator")` before styling a new widget class.
- Drag-and-drop needs `tkinterdnd2` *and* a `TkinterDnD.Tk()` root (created in `main()`). Both are optional everywhere else: the import is guarded, and `_build_ui` swallows the `TclError` from `drop_target_register` when the root is a plain `tk.Tk()` (tests, previews). Keep new code working without tkinterdnd2 installed.
