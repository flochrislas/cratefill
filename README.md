# Cratefill

A small desktop app that moves songs between CSV files, folders of music files,
and your YouTube Music playlists.

![Cratefill screenshot](https://raw.githubusercontent.com/flochrislas/cratefill/main/docs/screenshot.png)

- **Left pane:** load your songs, either from a CSV (artist + song name; extra
  columns are ignored) or from a folder of music files (**Load folder…** — the
  folder name + file name become the search query). You can also drag and drop
  a CSV file or a folder straight onto the song list. Click a column title to sort.
- **Right pane:** log in to YouTube Music and see your playlists.
- Select songs on the left, one or more playlists on the right, click **Add** —
  each song is searched on YouTube Music and added to every selected playlist.
- Cratefill won't *silently* add a song it isn't sure about. Every match is either
  **confident** (`✓`, added), **uncertain** (`?`, your choice) or **no match**
  (`✗`, skipped, with the reason given). When an uncertain match has close
  rivals, you can pick among them before adding.
- It also tries hard not to come back empty-handed. If the exact recording isn't
  on YouTube Music but another version of the same song is — a live take, a
  remix, an acoustic version, even another band's cover — you're offered that
  rather than nothing, with the difference spelled out ("this is the live
  version, not the one asked for"). When the exact version *is* there, it wins.
- `✗` is reserved for a genuinely different song: nothing came back that shares
  a word with the title you asked for. So `Cher — One` never becomes
  `Cherub — Someone`, and a different track by the right artist is never
  substituted.
- The **On ambiguous match** dropdown next to the Add button decides what happens
  to the uncertain ones: **Always ask** (the default — you get a Requested /
  Proposed / Reason prompt with Skip and Add), **Always skip**, or **Always add**.
  Your choice is remembered between runs. It never applies to `✗` results, and
  the *weakest* matches — a title that only loosely overlaps, or a song credited
  to a completely different artist — always ask, even on **Always add**.
- Nothing is written to a playlist until every decision is made, so closing the
  prompt cancels the whole import and leaves your playlists untouched.
- The reverse works too: select playlists and click **Export CSV…** to save each
  one as a CSV file with artist, track name, and album columns.

## Get it

- **Windows, no Python:** download `Cratefill.exe` from the
  [latest release](https://github.com/flochrislas/cratefill/releases/latest)
  and double-click it.
- **With Python (any OS):** `pipx install cratefill` (or `pip install cratefill`),
  then run `cratefill`. Add drag-and-drop with `pipx install "cratefill[dnd]"`.
- **From source:** see [Run](#run) below.

## Requirements

- Python 3.10+ (uses the built-in Tkinter GUI)
- [ytmusicapi](https://github.com/sigma67/ytmusicapi)
- [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) (song-title/artist similarity)
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) (optional — enables
  drag-and-drop; the app runs fine without it)

```
py -m pip install -r requirements.txt
```

## Run

Windows:

```
py -m cratefill
```

Linux (e.g. Ubuntu) — Tkinter is packaged separately from Python there, so install it once:

```
sudo apt install python3-tk
python3 -m pip install -r requirements.txt
python3 -m cratefill
```

Installed from PyPI (`pip install cratefill`), the `cratefill` command launches it directly.

## Logging in (first time)

YouTube Music has no public login API, so the app authenticates the way
[ytmusicapi browser auth](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html)
does — by reusing your browser session:

1. Open https://music.youtube.com in your browser, logged in to your account.
2. Press F12 → **Network** tab, then click around the page (e.g. Library).
3. Filter requests by `browse`, click one of the `browse?...` POST requests.
4. Copy the **request headers**:
   - **Firefox:** right-click the request → Copy Value → **Copy Request Headers**
   - **Chrome/Edge:** in the Headers panel, select everything under
     *Request Headers* and copy it.
5. In Cratefill, click **Log in…**, paste the headers, click **Log in**.

The session is saved to a `browser.json` in your own user profile, so subsequent
launches log in automatically. It stays valid until you log out of YouTube in
that browser.

| Platform | Location |
|---|---|
| Windows | `%APPDATA%\Cratefill\browser.json` |
| macOS | `~/Library/Application Support/Cratefill/browser.json` |
| Linux | `$XDG_CONFIG_HOME/cratefill/browser.json` (default `~/.config/cratefill/`) |

**`browser.json` contains your session cookies — don't share it.** On macOS and
Linux the file is created mode `0600` (owner-only) inside a `0700` directory.
Older versions kept it next to the app; that copy is moved to the new location
automatically on first launch.

## CSV format

Any CSV with an artist column and a song-name column works:

- Column headers are detected by name (`artist`/`artiste`, `title`/`titre`/`song`/`track`/`chanson`…).
- Without recognizable headers, the first column is taken as artist, the second as song name.
- Comma, semicolon, or tab delimiters are auto-detected; extra columns are ignored.
- An optional `station`/`radio`/`chaîne` column is displayed in the app — handy to
  remember where you heard a song — but is not used when searching YouTube Music.

See `sample.csv` for an example.

## Files

| File | Purpose |
|---|---|
| `cratefill/app.py` | Tkinter window, theme, dialogs, worker threads |
| `cratefill/matching.py` | Which search result answers a request, and how sure we are |
| `cratefill/policy.py` | What to do about an uncertain match, and remembering it |
| `cratefill/storage.py` | CSV import/export, music folders, data directory |
| `cratefill/youtube.py` | YouTube Music calls and credential handling |
| `run_cratefill.py` | Entry script used to build the Windows `.exe` |
| `tests/` | Test suite (`py -m pytest`) — no network or login needed |
| `sample.csv` | Example CSV |
| `browser.json` | Your saved login session — created on first login in your user profile, *not* here (see [Logging in](#logging-in-first-time)); keep private |
| `settings.json` | Your **On ambiguous match** preference, saved next to `browser.json` in your user profile |
| `RESEARCH.md` | Notes on the approaches considered |

## License

[GNU General Public License v3.0](LICENSE)
