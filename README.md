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
- The log pane shows what matched (`✓`), what matched only loosely (`?` — review these),
  and what wasn't found (`✗`).
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
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) (optional — enables
  drag-and-drop; the app runs fine without it)

```
py -m pip install -r requirements.txt
```

## Run

Windows:

```
py cratefill.py
```

Linux (e.g. Ubuntu) — Tkinter is packaged separately from Python there, so install it once:

```
sudo apt install python3-tk
python3 -m pip install -r requirements.txt
python3 cratefill.py
```

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

The session is saved to `browser.json` next to the app, so subsequent launches
log in automatically. It stays valid until you log out of YouTube in that
browser. **`browser.json` contains your session cookies — don't share it.**

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
| `cratefill.py` | The app (single file) |
| `sample.csv` | Example CSV |
| `browser.json` | Your saved login session (created on first login — keep private) |
| `RESEARCH.md` | Notes on the approaches considered |

## License

[GNU General Public License v3.0](LICENSE)
