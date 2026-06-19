# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Cratefill is a single-file Tkinter desktop app (`cratefill.py`) that loads songs from a CSV (artist + title) or from a folder of music files and adds them to the user's YouTube Music playlists via the unofficial [ytmusicapi](https://github.com/sigma67/ytmusicapi) library; it can also export playlists back to Artist/Title/Album CSV files. See `implementation-notes.md` for the full architecture walkthrough and design rationale; `RESEARCH.md` for why ytmusicapi was chosen over the official YouTube Data API.

## Commands

On this machine the bare `python` command is a broken Windows Store shim — **always use the `py` launcher**.

```powershell
py -m pip install -r requirements.txt   # ytmusicapi + tkinterdnd2 (optional, drag-and-drop)
py cratefill.py                        # run the app
```

There is no test suite or linter (packaging/release commands are under **Releasing**). Headless smoke test after UI changes:

```powershell
py -c "import tkinter as tk; from cratefill import CratefillApp; r = tk.Tk(); r.withdraw(); CratefillApp(r); r.update(); r.destroy(); print('OK')"
```

`read_songs_csv`, `read_songs_folder`, `pick_match`, `clean_pasted_headers`, `safe_filename`, and `write_playlist_csv` are pure functions — test them directly (e.g. against `sample.csv` or fixture folders/track dicts). Anything hitting YouTube Music (search, playlists, adding, exporting) requires a real logged-in session in `browser.json` and can only be tested manually.

## Releasing

Cratefill ships to **PyPI** (`pip install cratefill`) and as a **GitHub release** carrying a standalone Windows `.exe`. The version lives in **two** places that must stay identical: `__version__` in `cratefill.py` and `version` in `pyproject.toml`.

1. Bump the version in both files, commit, then tag and push:

   ```powershell
   git tag v0.2.0
   git push origin v0.2.0
   ```

   Pushing a `v*` tag triggers `.github/workflows/publish.yml`, which builds the sdist + wheel and publishes to PyPI over OIDC **trusted publishing** — no token is stored. (The one-time PyPI publisher config is already done: owner `flochrislas`, repo `cratefill`, workflow `publish.yml`, environment `pypi`.) Watch the run under the repo's Actions tab.

2. The Windows `.exe` is **not** built by CI (it needs a Windows runner) — build and attach it manually:

   ```powershell
   py -m PyInstaller --onefile --windowed --name Cratefill --collect-all tkinterdnd2 cratefill.py
   gh release create v0.2.0 dist/Cratefill.exe --title "Cratefill v0.2.0" --notes "..."
   ```

   `--collect-all tkinterdnd2` is required — without it the bundled tkdnd binaries are missing and the frozen app crashes at `TkinterDnD.Tk()`.

To inspect the dists before tagging: `py -m build` then `py -m twine check dist/*`. Manual PyPI upload fallback (needs a `pypi-…` token and an **interactive** terminal — it can't be backgrounded, twine prompts for the token): `py -m twine upload dist/cratefill-<ver>*`. Build artifacts (`build/`, `dist/`, `*.spec`) are gitignored.

## Architecture essentials

- Everything is in `cratefill.py`: module-level pure functions (`read_songs_csv`, `pick_match`), `LoginDialog`, and `CratefillApp`. Keep it single-file unless it grows substantially.
- **Threading rule:** network I/O runs in worker threads (`_worker` for adding, `_export_worker` for playlist→CSV export) that communicate only via `self.worker_queue`, drained on the main thread by `_poll_worker` (`root.after` loop). Never touch a Tk widget from a worker thread. A `("done", "refresh")` message refetches playlists; `("done", None)` doesn't.
- Selection mapping: song Treeview row iids are string indices into `self.songs`; playlist Listbox indices map into `self.playlists`. Preserve these mappings if adding sorting/filtering — column-click sorting (`sort_songs`) already does it right by only reordering rows with `tree.move`, never changing iids.
- Songs are `(artist, title, station)` tuples. The optional station column ("where I heard this") is display-only context: hidden via `displaycolumns` when the CSV has none, and never part of the YouTube Music search query.
- Auth is ytmusicapi browser auth: pasted request headers → `browser.json` next to the script. **`browser.json` holds the user's session cookies — never commit it** (add to `.gitignore` if this becomes a git repo).
- Searches are one call per song; playlist adds are batched (one `add_playlist_items` call per playlist with all videoIds), with `duplicates=False`. **YT Music fails such a batch atomically if even one song is already in the playlist** (and `duplicates=True` would add the duplicates), so on failure `_worker` fetches the playlist, filters out already-present videoIds, and retries once with the rest.

## Gotchas

- ytmusicapi is unofficial and tracks YT Music's private web API. If YT-side calls suddenly break, first try `py -m pip install -U ytmusicapi` and check the library's GitHub issues.
- CSV input is messy by design: the parser handles UTF-8/cp1252, comma/semicolon/tab, English+French header names (`ARTIST_HEADERS`/`TITLE_HEADERS`/`STATION_HEADERS` tuples), and headerless files. Don't simplify this away; extend the header tuples to support new column names. The station column is matched by header name only, never positionally.
- `exportselection=False` on the playlist Listbox is required — without it, clicking the song pane clears the playlist selection.
- Search results can have `videoId=None`; `pick_match` filters these.
- The UI is dark-only and must stay cross-platform (Windows + Linux): the theme is hand-rolled in `apply_dark_theme()` on top of the built-in `clam` theme — do not switch to platform themes (`vista`, `winnative`) or to sv-ttk (tried; it applies empty styles on this Python 3.14/Tk 8.6.15 build). When adding plain tk widgets (Text, Listbox), style them with `DARK_TEXT_STYLE`/`DARK_LIST_STYLE`; Listbox rejects `insertbackground` (that's why two dicts exist).
- `_screenshot_preview.py` renders the app with fake data and saves `ui_preview.png` (requires `pillow`) — use it to verify UI changes visually.
- Drag-and-drop needs `tkinterdnd2` *and* a `TkinterDnD.Tk()` root (created in `main()`). Both are optional everywhere else: the import is guarded, and `_build_ui` swallows the `TclError` from `drop_target_register` when the root is a plain `tk.Tk()` (tests, previews). Keep new code working without tkinterdnd2 installed.
