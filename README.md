# Cratefill

A small desktop app that adds songs from a CSV file to your YouTube Music playlists.

- **Left pane:** load a CSV with your songs (artist + song name; extra columns are ignored).
  Click a column title to sort.
- **Right pane:** log in to YouTube Music and see your playlists.
- Select songs on the left, one or more playlists on the right, click **Add** —
  each song is searched on YouTube Music and added to every selected playlist.
- The log pane shows what matched (`✓`), what matched only loosely (`?` — review these),
  and what wasn't found (`✗`).

## Requirements

- Python 3.10+ (uses the built-in Tkinter GUI — no extra GUI packages)
- [ytmusicapi](https://github.com/sigma67/ytmusicapi)

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
