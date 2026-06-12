"""Cratefill — add songs from a CSV file to YouTube Music playlists.

Left pane:  songs loaded from a CSV (artist + title columns, optional station
column shown for reference, extras ignored). Click a column title to sort.
Right pane: your YouTube Music playlists after logging in.
Select songs + playlists, click Add: each song is searched on YouTube Music
and added to every selected playlist. Results are reported in the log pane.
"""

import csv
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import ytmusicapi
from ytmusicapi import YTMusic

APP_DIR = Path(__file__).resolve().parent
AUTH_FILE = APP_DIR / "browser.json"

# Dark palette. The ttk side is themed by apply_dark_theme() on top of "clam"
# (the only built-in theme that renders identically on Windows and Linux);
# plain tk widgets (Text, Listbox) take these styles directly.
BG = "#1e1e1e"        # window / frame background
FIELD = "#141414"     # data areas: tree, listbox, text
BTN = "#333333"       # buttons, headings, scrollbar thumbs
BTN_ACTIVE = "#404040"
FG = "#e8e8e8"
FG_DIM = "#888888"
BORDER = "#3c3c3c"
ACCENT = "#0f4a8a"    # selection background
ACCENT_BAR = "#4a9eff" # progress bar fill

DARK_LIST_STYLE = dict(
    bg=FIELD,
    fg=FG,
    selectbackground=ACCENT,
    selectforeground="#ffffff",
    relief="flat",
    highlightthickness=1,
    highlightbackground=BORDER,
    highlightcolor=BORDER,
)
DARK_TEXT_STYLE = {**DARK_LIST_STYLE, "insertbackground": FG}


def apply_dark_theme(root):
    """Dark-style all ttk widgets on top of the cross-platform 'clam' theme."""
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(
        ".",
        background=BG, foreground=FG, fieldbackground=FIELD,
        bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
        troughcolor=FIELD, focuscolor=BORDER,
        selectbackground=ACCENT, selectforeground="#ffffff",
        insertcolor=FG,
    )
    style.configure("TButton", background=BTN, padding=(10, 5))
    style.map(
        "TButton",
        background=[("disabled", BG), ("pressed", "#2a2a2a"), ("active", BTN_ACTIVE)],
        foreground=[("disabled", FG_DIM)],
    )
    style.configure("Treeview", background=FIELD, fieldbackground=FIELD, rowheight=24)
    style.map(
        "Treeview",
        background=[("selected", ACCENT)],
        foreground=[("selected", "#ffffff")],
    )
    style.configure("Treeview.Heading", background=BTN, relief="flat", padding=4)
    style.map("Treeview.Heading", background=[("active", BTN_ACTIVE)])
    style.configure("TLabelframe", bordercolor=BORDER)
    style.configure("TLabelframe.Label", foreground=FG_DIM)
    style.configure(
        "TProgressbar",
        background=ACCENT_BAR, troughcolor=FIELD,
        bordercolor=BORDER, lightcolor=ACCENT_BAR, darkcolor=ACCENT_BAR,
    )
    style.configure(
        "Vertical.TScrollbar",
        background=BTN, troughcolor=BG, bordercolor=BG, arrowcolor=FG,
        relief="flat",
    )
    style.map("Vertical.TScrollbar", background=[("active", BTN_ACTIVE)])
    style.configure("Sash", sashthickness=6)


def enable_dark_title_bar(window):
    """Ask Windows (11) to draw this window's title bar in dark mode."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int),
        )
    except Exception:
        pass  # cosmetic only — never block startup over it

ARTIST_HEADERS = ("artist", "artiste", "interprete", "interprète")
TITLE_HEADERS = ("title", "titre", "song", "track", "chanson", "morceau", "name")
STATION_HEADERS = ("station", "radio", "chaine", "chaîne", "source")

# Song Treeview columns: (column id, heading label). The station column is
# only displayed when the loaded CSV actually has station values.
SONG_COLUMNS = (("artist", "Artist"), ("title", "Song"), ("station", "Station"))

LOGIN_INSTRUCTIONS = """\
To log in, Cratefill needs the request headers of your YouTube Music session:

1. Open https://music.youtube.com in your browser and make sure you are logged in.
2. Open the developer tools (F12) and select the Network tab.
3. Click on the YouTube Music page (e.g. on Library) so requests appear.
4. In the Network tab filter box, type:  browse
5. Click one of the "browse?..." requests, then find the Request Headers section.
   - Firefox: right-click the request > Copy Value > Copy Request Headers
   - Chrome/Edge: in the Headers panel, select everything under
     "Request Headers" and copy it (extra lines are ignored).
6. Paste the copied headers below and click Log in.

Your session is saved locally in browser.json next to the app, so you only
need to do this once (until you log out of YouTube in that browser)."""


def read_songs_csv(path):
    """Return a list of (artist, title, station) tuples from a CSV file.

    Detects the delimiter, and finds the columns by header name; falls back
    to artist/title in the first two columns when headers are unrecognized.
    The station (where the user heard the song) is optional, only picked up
    from a recognized header, and "" when absent.
    """
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows = [row for row in csv.reader(text.splitlines(), dialect) if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = [cell.strip().casefold() for cell in rows[0]]
    artist_col = next((i for i, h in enumerate(header) if h in ARTIST_HEADERS), None)
    title_col = next((i for i, h in enumerate(header) if h in TITLE_HEADERS), None)
    station_col = next((i for i, h in enumerate(header) if h in STATION_HEADERS), None)
    if artist_col is not None and title_col is not None:
        rows = rows[1:]
    else:
        artist_col, title_col = 0, 1
        # Drop the first row anyway if it looks like a header we just couldn't map.
        if header and any(h in ARTIST_HEADERS + TITLE_HEADERS for h in header):
            rows = rows[1:]
        else:
            station_col = None  # no header row, so no way to spot a station column

    songs = []
    for row in rows:
        if len(row) <= max(artist_col, title_col):
            continue
        artist = row[artist_col].strip()
        title = row[title_col].strip()
        station = row[station_col].strip() if station_col is not None and station_col < len(row) else ""
        if artist or title:
            songs.append((artist, title, station))
    return songs


def normalize(s):
    return "".join(c for c in s.casefold() if c.isalnum() or c.isspace()).strip()


def pick_match(results, artist, title):
    """Pick the best search result. Returns (result, confident) or (None, False)."""
    want_artist, want_title = normalize(artist), normalize(title)
    candidates = [r for r in results if r.get("videoId")]
    for r in candidates:
        got_title = normalize(r.get("title", ""))
        got_artists = [normalize(a.get("name", "")) for a in r.get("artists", [])]
        title_ok = want_title and (want_title in got_title or got_title in want_title)
        artist_ok = not want_artist or any(
            want_artist in a or a in want_artist for a in got_artists if a
        )
        if title_ok and artist_ok:
            return r, True
    if candidates:
        return candidates[0], False
    return None, False


# Plausible header name, optionally with the ":" prefix of HTTP/2 pseudo-headers
# (":authority") or the trailing ":" Chrome sometimes keeps on name lines.
HEADER_NAME_RE = re.compile(r":?[A-Za-z][A-Za-z0-9_-]*:?")


def clean_pasted_headers(raw):
    """Rebuild a {name: value} dict from request headers pasted out of DevTools.

    Accepts both the one-line format ("name: value", e.g. Firefox's Copy
    Request Headers) and the Chrome/Edge headers-panel selection, where names
    and values land on alternating lines. HTTP/2 pseudo-headers (":authority"
    etc.) and the decoded x-client-data protobuf block are dropped: fed
    straight to ytmusicapi they desync its parser into writing bogus headers
    (e.g. a request path as a header name) that make YouTube reject every
    request with a non-JSON error.
    """
    headers = {}
    pending = None  # header name waiting for its value on the next line
    in_decoded = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if in_decoded:
            in_decoded = line != "}"
        elif pending is not None:
            if not pending.startswith(":"):
                headers[pending] = line
            pending = None
        elif line.startswith("Decoded:"):
            in_decoded = True
        else:
            name, sep, value = line.partition(":")
            if sep and value.strip() and HEADER_NAME_RE.fullmatch(name):
                headers[name.lower()] = value.strip()
            elif HEADER_NAME_RE.fullmatch(line):
                # Name alone on its line; pseudo-header names keep their ":"
                # so the pair is consumed but not stored. Anything else
                # (request line, protobuf leftovers) is ignored.
                pending = line.rstrip(":").lower()
    return headers


class LoginDialog(tk.Toplevel):
    """Dialog asking the user to paste their music.youtube.com request headers."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Log in to YouTube Music")
        self.geometry("700x560")
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        enable_dark_title_bar(self)
        self.success = False

        ttk.Label(self, text=LOGIN_INSTRUCTIONS, justify="left", wraplength=660).pack(
            padx=12, pady=(12, 8), anchor="w"
        )
        self.headers_text = tk.Text(self, height=10, wrap="none", **DARK_TEXT_STYLE)
        self.headers_text.pack(fill="both", expand=True, padx=12)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=10)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Log in", command=self.submit).pack(side="right", padx=(0, 8))

    def submit(self):
        raw = self.headers_text.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("Cratefill", "Paste the request headers first.", parent=self)
            return
        headers = clean_pasted_headers(raw)
        if "cookie" not in headers:
            messagebox.showerror(
                "Cratefill",
                "No cookie found in the pasted text — make sure you copy the whole\n"
                "Request Headers section of a music.youtube.com request.",
                parent=self,
            )
            return
        # Some requests omit it; 0 is the default Google account. The
        # validation call below still catches a wrong guess.
        headers.setdefault("x-goog-authuser", "0")
        try:
            ytmusicapi.setup(
                filepath=str(AUTH_FILE),
                headers_raw="\n".join(f"{k}: {v}" for k, v in headers.items()),
            )
            YTMusic(str(AUTH_FILE)).get_library_playlists(limit=1)  # validate
        except Exception as e:
            AUTH_FILE.unlink(missing_ok=True)
            messagebox.showerror(
                "Cratefill",
                "Login failed — the pasted headers were not accepted.\n\n"
                f"Details: {e}",
                parent=self,
            )
            return
        self.success = True
        self.destroy()


class CratefillApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Cratefill — CSV to YouTube Music")
        self.root.geometry("1080x680")

        self.yt = None
        self.songs = []  # list of (artist, title, station)
        self.song_sort = (None, False)  # (column id, descending?)
        self.playlists = []  # list of dicts from get_library_playlists
        self.worker_queue = queue.Queue()
        self.working = False

        self._build_ui()
        self.root.after(100, self._poll_worker)
        if AUTH_FILE.exists():
            self._connect(silent=True)

    # ---------- UI construction ----------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(main, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # Left pane: songs
        left = ttk.Frame(panes, padding=(0, 0, 8, 0))
        panes.add(left, weight=3)

        left_top = ttk.Frame(left)
        left_top.pack(fill="x", pady=(0, 6))
        ttk.Button(left_top, text="Load CSV…", command=self.load_csv).pack(side="left")
        self.csv_label = ttk.Label(left_top, text="No file loaded")
        self.csv_label.pack(side="left", padx=8)
        ttk.Button(left_top, text="Select all", command=lambda: self.song_tree.selection_set(
            self.song_tree.get_children())).pack(side="right")

        self.song_tree = ttk.Treeview(
            left,
            columns=tuple(col for col, _ in SONG_COLUMNS),
            displaycolumns=("artist", "title"),
            show="headings",
            selectmode="extended",
        )
        for col, label in SONG_COLUMNS:
            self.song_tree.heading(col, text=label, command=lambda c=col: self.sort_songs(c))
        self.song_tree.column("artist", width=200)
        self.song_tree.column("title", width=260)
        self.song_tree.column("station", width=120)
        song_scroll = ttk.Scrollbar(left, orient="vertical", command=self.song_tree.yview)
        self.song_tree.configure(yscrollcommand=song_scroll.set)
        self.song_tree.pack(side="left", fill="both", expand=True)
        song_scroll.pack(side="right", fill="y")

        # Right pane: account + playlists
        right = ttk.Frame(panes, padding=(8, 0, 0, 0))
        panes.add(right, weight=2)

        right_top = ttk.Frame(right)
        right_top.pack(fill="x", pady=(0, 6))
        self.login_button = ttk.Button(right_top, text="Log in…", command=self.login)
        self.login_button.pack(side="left")
        ttk.Button(right_top, text="Refresh", command=self.refresh_playlists).pack(side="left", padx=6)
        self.account_label = ttk.Label(right_top, text="Not logged in")
        self.account_label.pack(side="left", padx=8)

        self.playlist_list = tk.Listbox(
            right, selectmode="extended", exportselection=False, **DARK_LIST_STYLE
        )
        playlist_scroll = ttk.Scrollbar(right, orient="vertical", command=self.playlist_list.yview)
        self.playlist_list.configure(yscrollcommand=playlist_scroll.set)
        self.playlist_list.pack(side="left", fill="both", expand=True)
        playlist_scroll.pack(side="right", fill="y")

        # Bottom: action button, progress, log
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(8, 0))
        self.add_button = ttk.Button(
            bottom, text="Add selected songs to selected playlist(s)", command=self.add_songs
        )
        self.add_button.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        log_frame = ttk.LabelFrame(main, text="Log", padding=4)
        log_frame.pack(fill="both", pady=(8, 0))
        self.log_text = tk.Text(
            log_frame, height=9, state="disabled", wrap="word", **DARK_TEXT_STYLE
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- Left pane: CSV ----------

    def load_csv(self):
        path = filedialog.askopenfilename(
            title="Open songs CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.songs = read_songs_csv(path)
        except Exception as e:
            messagebox.showerror("Cratefill", f"Could not read CSV:\n{e}")
            return
        self.populate_song_tree()
        self.csv_label.configure(text=f"{Path(path).name} — {len(self.songs)} songs")
        self.log(f"Loaded {len(self.songs)} songs from {path}")

    def populate_song_tree(self):
        """(Re)fill the tree from self.songs, in CSV order.

        Row iids are string indices into self.songs — selection and sorting
        rely on that mapping. The station column only shows when used.
        """
        self.song_tree.delete(*self.song_tree.get_children())
        for i, song in enumerate(self.songs):
            self.song_tree.insert("", "end", iid=str(i), values=song)
        has_station = any(station for _, _, station in self.songs)
        self.song_tree.configure(
            displaycolumns=("artist", "title", "station") if has_station else ("artist", "title")
        )
        self.song_sort = (None, False)
        for col, label in SONG_COLUMNS:
            self.song_tree.heading(col, text=label)

    def sort_songs(self, col):
        """Sort rows by a column; clicking the same column again reverses.

        Rows are reordered in place with tree.move, so iids keep pointing
        into self.songs and the current selection survives.
        """
        if not self.songs:
            return
        prev_col, descending = self.song_sort
        descending = not descending if col == prev_col else False
        self.song_sort = (col, descending)
        value_index = [c for c, _ in SONG_COLUMNS].index(col)
        order = sorted(
            self.song_tree.get_children(),
            key=lambda iid: self.songs[int(iid)][value_index].casefold(),
            reverse=descending,
        )
        for pos, iid in enumerate(order):
            self.song_tree.move(iid, "", pos)
        for c, label in SONG_COLUMNS:
            arrow = (" ▼" if descending else " ▲") if c == col else ""
            self.song_tree.heading(c, text=label + arrow)

    # ---------- Right pane: account ----------

    def login(self):
        dialog = LoginDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.success:
            self._connect()

    def _connect(self, silent=False):
        try:
            self.yt = YTMusic(str(AUTH_FILE))
        except Exception as e:
            self.yt = None
            if not silent:
                messagebox.showerror("Cratefill", f"Could not use saved login:\n{e}")
            return
        self.account_label.configure(text="Logged in")
        self.login_button.configure(text="Re-log in…")
        self.refresh_playlists()

    def refresh_playlists(self):
        if not self.yt:
            self.log("Not logged in — click 'Log in…' first.")
            return
        try:
            self.playlists = self.yt.get_library_playlists(limit=None)
        except Exception as e:
            self.log(f"Could not fetch playlists: {e}")
            self.account_label.configure(text="Login expired? Re-log in.")
            return
        self.playlist_list.delete(0, "end")
        for pl in self.playlists:
            count = pl.get("count")
            label = pl["title"] + (f"  ({count} tracks)" if count is not None else "")
            self.playlist_list.insert("end", label)
        self.log(f"Found {len(self.playlists)} playlists.")

    # ---------- Add songs ----------

    def add_songs(self):
        if self.working:
            return
        if not self.yt:
            messagebox.showwarning("Cratefill", "Log in to YouTube Music first.")
            return
        selected_songs = [self.songs[int(iid)] for iid in self.song_tree.selection()]
        selected_playlists = [self.playlists[i] for i in self.playlist_list.curselection()]
        if not selected_songs:
            messagebox.showwarning("Cratefill", "Select at least one song on the left.")
            return
        if not selected_playlists:
            messagebox.showwarning("Cratefill", "Select at least one playlist on the right.")
            return

        self.working = True
        self.add_button.configure(state="disabled")
        self.progress.configure(maximum=len(selected_songs) + len(selected_playlists), value=0)
        self.log(
            f"--- Adding {len(selected_songs)} song(s) to "
            f"{len(selected_playlists)} playlist(s) ---"
        )
        threading.Thread(
            target=self._worker, args=(selected_songs, selected_playlists), daemon=True
        ).start()

    def _worker(self, songs, playlists):
        """Background thread: search every song, then add matches to each playlist."""
        put = self.worker_queue.put
        video_ids = []
        not_found = 0
        for artist, title, _station in songs:  # station is context for the user, not a search term
            query = f"{artist} {title}".strip()
            try:
                results = self.yt.search(query, filter="songs", limit=5)
            except Exception as e:
                put(("log", f"✗ {artist} — {title}: search failed ({e})"))
                put(("step", None))
                not_found += 1
                continue
            match, confident = pick_match(results, artist, title)
            if match is None:
                put(("log", f"✗ {artist} — {title}: no match found"))
                not_found += 1
            else:
                video_ids.append(match["videoId"])
                found_artists = ", ".join(a.get("name", "") for a in match.get("artists", []))
                if confident:
                    put(("log", f"✓ {artist} — {title}"))
                else:
                    put(("log", f"? {artist} — {title}: uncertain match "
                                f"→ {found_artists} — {match.get('title')}"))
            put(("step", None))

        for pl in playlists:
            if not video_ids:
                put(("step", None))
                continue
            try:
                result = self.yt.add_playlist_items(pl["playlistId"], video_ids, duplicates=False)
                status = result.get("status", "") if isinstance(result, dict) else str(result)
                if "SUCCEEDED" in str(status):
                    put(("log", f"→ Added {len(video_ids)} song(s) to '{pl['title']}'"))
                else:
                    put(("log", f"→ '{pl['title']}': {status} "
                                "(some songs may be duplicates or the playlist is not editable)"))
            except Exception as e:
                put(("log", f"→ Failed to add to '{pl['title']}': {e}"))
            put(("step", None))

        summary = f"--- Done. {len(video_ids)} matched, {not_found} not found. ---"
        put(("log", summary))
        put(("done", None))

    def _poll_worker(self):
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "step":
                    self.progress.step(1)
                elif kind == "done":
                    self.working = False
                    self.add_button.configure(state="normal")
                    self.progress.configure(value=0)
                    self.refresh_playlists()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_worker)


def main():
    root = tk.Tk()
    apply_dark_theme(root)
    enable_dark_title_bar(root)
    CratefillApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
