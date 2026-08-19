"""Cratefill — move songs between CSV files, folders and YouTube Music playlists.

Left pane:  songs loaded from a CSV (artist + title columns, optional station
column shown for reference, extras ignored) or from a folder of music files
(folder name + file name). Click a column title to sort.
Right pane: your YouTube Music playlists after logging in.
Select songs + playlists, click Add: each song is searched on YouTube Music
and added to every selected playlist. Results are reported in the Messages pane.
The reverse also works: select playlists and click "Export CSV…" to save each
one as an Artist/Title/Album CSV file.
"""

import csv
import os
import queue
import re
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import ytmusicapi
from ytmusicapi import YTMusic

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # optional: without it the app works, minus drag and drop
    DND_FILES = TkinterDnD = None

__version__ = "0.1.1"

def user_data_dir():
    """Per-user directory for browser.json, following each platform's convention.

    Deliberately *not* next to the script or the .exe: that location can be
    read-only (system-wide installs), gets wiped on every launch under
    PyInstaller --onefile, and invites copying the session cookies around
    together with the executable.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / "Cratefill"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cratefill"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "cratefill"


AUTH_FILE = user_data_dir() / "browser.json"
# Where browser.json used to live in earlier versions; migrated away on startup.
LEGACY_AUTH_FILE = Path(
    sys.executable if getattr(sys, "frozen", False) else __file__
).resolve().parent / "browser.json"


def secure_auth_dir():
    """Create the data directory, owner-only on POSIX."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            AUTH_FILE.parent.chmod(0o700)
        except OSError:
            pass  # best effort; the file mode below is what actually matters


def secure_auth_file(path=None):
    """Restrict a credentials file to the owner — ytmusicapi writes it with the
    process umask, which commonly leaves it world-readable."""
    path = AUTH_FILE if path is None else Path(path)
    if os.name == "posix" and path.exists():
        try:
            path.chmod(0o600)
        except OSError:
            pass


def migrate_legacy_auth_file():
    """Move an older browser.json into the user data dir, so upgrading
    doesn't force a re-login and no readable copy is left behind."""
    if LEGACY_AUTH_FILE == AUTH_FILE or not LEGACY_AUTH_FILE.exists():
        return False
    try:
        secure_auth_dir()
        if not AUTH_FILE.exists():
            # Copy through os.open rather than replace()/shutil.move: the old and
            # new locations are often on different filesystems, and this way the
            # new file is never momentarily readable by anyone else.
            data = LEGACY_AUTH_FILE.read_bytes()
            fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        LEGACY_AUTH_FILE.unlink()  # leave no readable copy behind
        secure_auth_file()
        return True
    except OSError:
        return False  # unwritable data dir: leave the old file alone, user re-logs in

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
READY = "#3ddc84"     # "you can go now": Add button outline once songs + playlists are picked

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
    style.configure("TButton", background=BTN, padding=(10, 5), borderwidth=2)
    style.map(
        "TButton",
        background=[("disabled", BG), ("pressed", "#2a2a2a"), ("active", BTN_ACTIVE)],
        foreground=[("disabled", FG_DIM)],
    )
    # Same geometry as TButton (borderwidth included) so swapping styles never
    # shifts the layout — only the border and label colour change.
    style.configure("Ready.TButton", bordercolor=READY, lightcolor=READY, darkcolor=READY,
                    foreground=READY)
    style.map(
        "Ready.TButton",
        background=[("disabled", BG), ("pressed", "#2a2a2a"), ("active", BTN_ACTIVE)],
        foreground=[("disabled", FG_DIM), ("active", READY)],
        bordercolor=[("disabled", BORDER)],
        lightcolor=[("disabled", BG)],
        darkcolor=[("disabled", BG)],
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

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}

# Song Treeview columns: (column id, heading label). The station column is
# only displayed when the loaded CSV actually has station values.
SONG_COLUMNS = (("artist", "Artist"), ("title", "Song"), ("station", "Station"))

LOGIN_INSTRUCTIONS = f"""\
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

Your session is saved locally, for your user account only, in
{AUTH_FILE}
so you only need to do this once (until you log out of YouTube in that
browser)."""

# Shown in the Messages pane at startup, so the app never opens on a blank window.
HELP_TEXT = """\
How to use:

1. Load a list of songs from a CSV file or a folder.
2. Log into YouTube Music and select a playlist.
3. Select the songs you want to add to this playlist.
4. Click the big "Add selected songs to selected playlist(s)" button.

You can also export the list of songs from a selected playlist into a CSV file."""


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


def read_songs_folder(path):
    """Return (artist, title, station) tuples from a folder of music files.

    The folder name fills the artist column and the file name (without
    extension) the title column, so the YouTube Music search query becomes
    "folder name + file name" — works best for folders named after an artist,
    an album, or a station whose files are "Artist - Title.ext".
    """
    folder = Path(path)
    artist = " ".join(folder.name.split())
    return [
        (artist, " ".join(f.stem.split()), "")
        for f in sorted(folder.iterdir())
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    ]


def safe_filename(name):
    """Turn a playlist title into a usable Windows/Linux file name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "playlist"


def write_playlist_csv(title, tracks, dest_dir):
    """Write one playlist to '<title>.csv' in dest_dir and return the Path.

    tracks is the "tracks" list of ytmusicapi's get_playlist(). Columns are
    Artist, Title, Album (album is often missing — then ""). Existing files
    are never overwritten; a " (2)", " (3)"… suffix is added instead.
    """
    base = safe_filename(title)
    path = Path(dest_dir) / f"{base}.csv"
    n = 2
    while path.exists():
        path = Path(dest_dir) / f"{base} ({n}).csv"
        n += 1
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Artist", "Title", "Album"])
        for t in tracks:
            artists = ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name"))
            album = (t.get("album") or {}).get("name", "")
            writer.writerow([artists, t.get("title", ""), album])
    return path


def normalize(s):
    """Casefold and strip punctuation. Tolerates None: YT Music leaves fields
    like "title" or an artist "name" out (or null) on some results."""
    return "".join(c for c in (s or "").casefold() if c.isalnum() or c.isspace()).strip()


def pick_match(results, artist, title):
    """Pick the best search result. Returns (result, confident) or (None, False).

    Defensive about the shape of `results`: it comes straight from an
    unofficial API, where "artists" can be absent, null, or hold entries
    without a name. A TypeError here used to kill the whole worker thread.
    """
    want_artist, want_title = normalize(artist), normalize(title)
    candidates = [
        r for r in (results or []) if isinstance(r, dict) and r.get("videoId")
    ]
    for r in candidates:
        got_title = normalize(r.get("title"))
        got_artists = [
            normalize(a.get("name"))
            for a in (r.get("artists") or [])
            if isinstance(a, dict)
        ]
        # Both titles must be non-empty: "" is a substring of everything, so a
        # result with no title would otherwise pass as a confident match.
        title_ok = bool(want_title and got_title) and (
            want_title in got_title or got_title in want_title
        )
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
        self.validating = False
        self.result_queue = queue.Queue()  # worker thread → _poll_validation

        ttk.Label(self, text=LOGIN_INSTRUCTIONS, justify="left", wraplength=660).pack(
            padx=12, pady=(12, 8), anchor="w"
        )
        self.headers_text = tk.Text(self, height=10, wrap="none", **DARK_TEXT_STYLE)
        self.headers_text.pack(fill="both", expand=True, padx=12)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=10)
        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="right")
        self.submit_button = ttk.Button(buttons, text="Log in", command=self.submit)
        self.submit_button.pack(side="right", padx=(0, 8))
        self.status_label = ttk.Label(buttons, text="")
        self.status_label.pack(side="left")
        # Don't let the window close while a validation thread still owns the
        # staged credentials file.
        self.protocol("WM_DELETE_WINDOW", lambda: None if self.validating else self.destroy())

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
        # Validating means a network round trip, so it runs on a worker thread:
        # doing it here would freeze the dialog until YouTube answers.
        self.validating = True
        self.status_label.configure(text="Checking with YouTube Music…")
        self.submit_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        threading.Thread(target=self._validate_worker, args=(headers,), daemon=True).start()
        self.after(100, self._poll_validation)

    def _validate_worker(self, headers):
        """Worker thread: write the credentials, validate them, swap them in.

        Reports the outcome on self.result_queue — None for success, otherwise
        the exception. Touches no widget.
        """
        try:
            secure_auth_dir()
            # Stage the new credentials in a sibling file and only swap them in
            # once they are known to work, so a mistyped re-login leaves the
            # existing session untouched. Same directory means same filesystem,
            # which is what makes os.replace atomic.
            fd, tmp_name = tempfile.mkstemp(
                dir=AUTH_FILE.parent, prefix=".browser-", suffix=".json"
            )
            os.close(fd)  # mkstemp created it 0600; setup() rewrites it in place
            staged = Path(tmp_name)
            try:
                ytmusicapi.setup(
                    filepath=str(staged),
                    headers_raw="\n".join(f"{k}: {v}" for k, v in headers.items()),
                )
                secure_auth_file(staged)  # ytmusicapi writes with the process umask
                YTMusic(str(staged)).get_library_playlists(limit=1)  # validate
                os.replace(staged, AUTH_FILE)  # atomic: never a half-written session
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
        except Exception as e:
            self.result_queue.put(e)
        else:
            self.result_queue.put(None)

    def _poll_validation(self):
        """Main thread: wait for _validate_worker without blocking the dialog."""
        if not self.winfo_exists():
            return
        try:
            error = self.result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_validation)
            return
        self.validating = False
        if error is None:
            self.success = True
            self.destroy()
            return
        self.status_label.configure(text="")
        self.submit_button.configure(state="normal")
        self.cancel_button.configure(state="normal")
        kept = " Your previous session is still in place." if AUTH_FILE.exists() else ""
        messagebox.showerror(
            "Cratefill",
            f"Login failed — the pasted headers were not accepted.{kept}\n\n"
            f"Details: {error}",
            parent=self,
        )


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
        self.show_help()
        self.root.after(100, self._poll_worker)
        if migrate_legacy_auth_file():
            self.log(f"Moved your saved session to {AUTH_FILE.parent}")
        secure_auth_file()  # tighten a file written by an older version
        if AUTH_FILE.exists():
            self._connect(silent=True)

    # ---------- UI construction ----------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(main, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # Left pane: songs
        left = ttk.LabelFrame(panes, text="Songs list", padding=4)
        panes.add(left, weight=3)

        left_top = ttk.Frame(left)
        left_top.pack(fill="x", pady=(0, 6))
        ttk.Button(left_top, text="Load CSV…", command=self.load_csv).pack(side="left")
        ttk.Button(left_top, text="Load folder…", command=self.load_folder).pack(side="left", padx=6)
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
        self.song_tree.bind("<<TreeviewSelect>>", lambda _e: self.refresh_add_button())
        song_scroll = ttk.Scrollbar(left, orient="vertical", command=self.song_tree.yview)
        self.song_tree.configure(yscrollcommand=song_scroll.set)
        self.song_tree.pack(side="left", fill="both", expand=True)
        song_scroll.pack(side="right", fill="y")

        if DND_FILES:
            try:
                self.song_tree.drop_target_register(DND_FILES)
                self.song_tree.dnd_bind("<<Drop>>", self._on_drop)
            except tk.TclError:
                pass  # root isn't a TkinterDnD.Tk (tests/previews) — no DnD, app still works

        # Right pane: account + playlists
        right = ttk.LabelFrame(panes, text="YouTube Music", padding=4)
        panes.add(right, weight=2)

        right_top = ttk.Frame(right)
        right_top.pack(fill="x", pady=(0, 6))
        self.login_button = ttk.Button(right_top, text="Log in…", command=self.login)
        self.login_button.pack(side="left")
        self.refresh_button = ttk.Button(
            right_top, text="Refresh", command=self.refresh_playlists
        )
        self.refresh_button.pack(side="left", padx=6)
        self.export_button = ttk.Button(
            right_top, text="Export CSV…", command=self.export_playlists
        )
        self.export_button.pack(side="left")
        self.account_label = ttk.Label(right_top, text="Not logged in")
        self.account_label.pack(side="left", padx=8)

        self.playlist_list = tk.Listbox(
            right, selectmode="extended", exportselection=False, **DARK_LIST_STYLE
        )
        self.playlist_list.bind("<<ListboxSelect>>", lambda _e: self.refresh_add_button())
        playlist_scroll = ttk.Scrollbar(right, orient="vertical", command=self.playlist_list.yview)
        self.playlist_list.configure(yscrollcommand=playlist_scroll.set)
        self.playlist_list.pack(side="left", fill="both", expand=True)
        playlist_scroll.pack(side="right", fill="y")

        # Bottom: action button, progress, messages
        bottom = ttk.LabelFrame(main, text="Process", padding=4)
        bottom.pack(fill="x", pady=(8, 0))
        self.add_button = ttk.Button(
            bottom, text="Add selected songs to selected playlist(s)", command=self.add_songs
        )
        self.add_button.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        log_frame = ttk.LabelFrame(main, text="Messages", padding=4)
        log_frame.pack(fill="both", pady=(8, 0))
        self.log_text = tk.Text(
            log_frame, height=9, state="disabled", wrap="word", **DARK_TEXT_STYLE
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        # Everything that talks to YouTube Music, disabled for the duration of
        # a job by _start_work — see there for why Log in and Refresh count.
        self.busy_controls = (
            self.add_button, self.export_button, self.login_button, self.refresh_button,
        )

    def refresh_add_button(self):
        """Outline the Add button in green once there is something to add.

        Bound to <<TreeviewSelect>>/<<ListboxSelect>>, and called directly
        wherever code changes a selection: the Listbox fires no virtual event
        for programmatic selection changes, and neither pane fires one when
        its contents are wiped and refilled.
        """
        ready = bool(self.song_tree.selection()) and bool(self.playlist_list.curselection())
        self.add_button.configure(style="Ready.TButton" if ready else "TButton")

    def show_help(self):
        self.log(HELP_TEXT)

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
        if path:
            self.load_csv_path(path)

    def load_csv_path(self, path):
        try:
            self.songs = read_songs_csv(path)
        except Exception as e:
            messagebox.showerror("Cratefill", f"Could not read CSV:\n{e}")
            return
        self.populate_song_tree()
        self.csv_label.configure(text=f"{Path(path).name} — {len(self.songs)} songs")
        self.log(f"Loaded {len(self.songs)} songs from {path}")

    def load_folder(self):
        path = filedialog.askdirectory(title="Open a folder of music files")
        if path:
            self.load_folder_path(path)

    def load_folder_path(self, path):
        songs = read_songs_folder(path)
        if not songs:
            messagebox.showwarning("Cratefill", "No music files found in that folder.")
            return
        self.songs = songs
        self.populate_song_tree()
        self.csv_label.configure(text=f"{Path(path).name} — {len(self.songs)} songs")
        self.log(f"Loaded {len(self.songs)} music files from {path}")

    def _on_drop(self, event):
        """Handle a file/folder dropped onto the song list."""
        paths = [Path(p) for p in self.song_tree.tk.splitlist(event.data)]
        if not paths:
            return
        if len(paths) > 1:
            self.log("Multiple items dropped — loading only the first one.")
        path = paths[0]
        if path.is_dir():
            self.load_folder_path(str(path))
        elif path.suffix.lower() in (".csv", ".txt"):
            self.load_csv_path(str(path))
        else:
            messagebox.showwarning(
                "Cratefill", "Drop a .csv file (or a folder of music files)."
            )

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
        self.refresh_add_button()

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
        if self.working:  # the button is disabled too; belt and braces
            return
        dialog = LoginDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.success:
            self._connect()

    def _connect(self, silent=False):
        """Build the YTMusic client and load the playlists, off the UI thread."""
        if self.working:
            return
        self._start_work()
        self.log("Connecting to YouTube Music…")
        threading.Thread(target=self._connect_worker, args=(silent,), daemon=True).start()

    def _connect_worker(self, silent):
        """Worker thread: open the saved session, then fetch its playlists."""
        put = self.worker_queue.put
        try:
            yt = YTMusic(str(AUTH_FILE))
        except Exception as e:
            put(("connect_failed", (silent, str(e))))
        else:
            put(("connected", yt))
            self._put_playlists(yt)
        finally:
            put(("done", None))

    def refresh_playlists(self):
        # Never hit the API from here while a worker owns the client. _poll_worker
        # calls _end_work() (clearing self.working) before starting this.
        if self.working:
            return
        if not self.yt:
            self.log("Not logged in — click 'Log in…' first.")
            return
        self._start_work()
        threading.Thread(target=self._refresh_worker, args=(self.yt,), daemon=True).start()

    def _refresh_worker(self, yt):
        try:
            self._put_playlists(yt)
        finally:
            self.worker_queue.put(("done", None))

    def _put_playlists(self, yt):
        """Worker thread: fetch the playlists and hand them to the UI.

        Swallows its own errors so callers can use it as a final step without
        losing whatever they already reported.
        """
        try:
            self.worker_queue.put(("playlists", yt.get_library_playlists(limit=None)))
        except Exception as e:
            self.worker_queue.put(("log", f"Could not fetch playlists: {e}"))
            self.worker_queue.put(("account", "Login expired? Re-log in."))

    def _show_playlists(self, playlists):
        """Main thread: refill the playlist Listbox."""
        self.playlists = playlists
        self.playlist_list.delete(0, "end")
        for pl in self.playlists:
            count = pl.get("count")
            label = pl["title"] + (f"  ({count} tracks)" if count is not None else "")
            self.playlist_list.insert("end", label)
        self.refresh_add_button()
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

        self._start_work(maximum=len(selected_songs) + len(selected_playlists))
        self.log(
            f"--- Adding {len(selected_songs)} song(s) to "
            f"{len(selected_playlists)} playlist(s) ---"
        )
        # Snapshot the client: the worker must keep using the account it started
        # with, even if self.yt is replaced later.
        threading.Thread(
            target=self._worker,
            args=(self.yt, selected_songs, selected_playlists),
            daemon=True,
        ).start()

    def export_playlists(self):
        """Save each selected playlist as an Artist/Title/Album CSV file."""
        if self.working:
            return
        if not self.yt:
            messagebox.showwarning("Cratefill", "Log in to YouTube Music first.")
            return
        selected = [self.playlists[i] for i in self.playlist_list.curselection()]
        if not selected:
            messagebox.showwarning("Cratefill", "Select at least one playlist on the right.")
            return
        dest = filedialog.askdirectory(title="Choose where to save the CSV file(s)")
        if not dest:
            return
        self._start_work(maximum=len(selected))
        self.log(f"--- Exporting {len(selected)} playlist(s) to {dest} ---")
        threading.Thread(
            target=self._export_worker, args=(self.yt, selected, dest), daemon=True
        ).start()  # snapshot self.yt — see add_songs

    def _start_work(self, maximum=None):
        """Lock every YouTube Music control for the duration of a job.

        With no `maximum` the job has no countable steps (connect, refresh) and
        the progress bar animates instead, so a slow network reads as "working"
        rather than "hung".

        Log in and Refresh are locked too, not just Add/Export: logging in
        replaces self.yt, which would switch accounts under a running worker,
        and Refresh would drive the same YTMusic client (and its requests
        session) from two threads at once.
        """
        self.working = True
        for button in self.busy_controls:
            button.configure(state="disabled")
        if maximum is None:
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
        else:
            self.progress.configure(mode="determinate", maximum=maximum, value=0)

    def _end_work(self):
        self.working = False
        for button in self.busy_controls:
            button.configure(state="normal")
        self.progress.stop()  # no-op in determinate mode
        self.progress.configure(mode="determinate", value=0)

    def _worker(self, yt, songs, playlists):
        """Background thread entry point: always reports completion.

        The Add/Export buttons stay disabled until a ("done", …) message
        arrives, so a worker that dies on an unexpected error — malformed API
        data, say — would leave the UI unusable until restart. Hence the
        try/finally: the thread cannot exit without re-enabling the UI.
        """
        try:
            self._add_to_playlists(yt, songs, playlists)
        except Exception as e:
            self.worker_queue.put(
                ("log", f"✗ Unexpected error while adding: {type(e).__name__}: {e}")
            )
        finally:
            self._put_playlists(yt)  # the track counts just changed — refetch here,
            self.worker_queue.put(("done", None))  # still off the UI thread

    def _add_to_playlists(self, yt, songs, playlists):
        """Search every song, then add the matches to each playlist.

        `yt` is passed in rather than read off self, so the job stays bound to
        one account even if the user logs into another one afterwards.
        """
        put = self.worker_queue.put
        video_ids = []
        not_found = 0
        for artist, title, _station in songs:  # station is context for the user, not a search term
            query = f"{artist} {title}".strip()
            try:
                results = yt.search(query, filter="songs", limit=5)
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
                found_artists = ", ".join(
                    a.get("name") or ""
                    for a in (match.get("artists") or [])
                    if isinstance(a, dict)
                )
                if confident:
                    put(("log", f"✓ {artist} — {title}"))
                else:
                    put(("log", f"? {artist} — {title}: uncertain match "
                                f"→ {found_artists} — {match.get('title')}"))
            put(("step", None))

        video_ids = list(dict.fromkeys(video_ids))  # two rows can match the same YT song

        def status_of(result):
            return str(result.get("status", "")) if isinstance(result, dict) else str(result)

        for pl in playlists:
            if not video_ids:
                put(("step", None))
                continue
            try:
                # YT Music rejects the whole batch if even one song is already in
                # the playlist (no items get added), so on failure drop the songs
                # it already contains and retry with the rest.
                to_add = video_ids
                skipped = 0
                result = yt.add_playlist_items(pl["playlistId"], to_add, duplicates=False)
                if "SUCCEEDED" not in status_of(result):
                    existing = {
                        t.get("videoId")
                        for t in yt.get_playlist(pl["playlistId"], limit=None).get("tracks", [])
                    }
                    to_add = [v for v in video_ids if v not in existing]
                    skipped = len(video_ids) - len(to_add)
                    if not to_add:
                        put(("log", f"→ '{pl['title']}': all {len(video_ids)} song(s) "
                                    "are already in the playlist — nothing to add"))
                        put(("step", None))
                        continue
                    result = yt.add_playlist_items(pl["playlistId"], to_add, duplicates=False)
                status = status_of(result)
                if "SUCCEEDED" in status:
                    message = f"→ Added {len(to_add)} song(s) to '{pl['title']}'"
                    if skipped:
                        message += f" ({skipped} already there, skipped)"
                    put(("log", message))
                else:
                    put(("log", f"→ '{pl['title']}': {status} (playlist not editable, or YT Music "
                                "sees some of these songs as duplicates under different ids)"))
            except Exception as e:
                put(("log", f"→ Failed to add to '{pl['title']}': {e}"))
            put(("step", None))

        summary = f"--- Done. {len(video_ids)} matched, {not_found} not found. ---"
        put(("log", summary))

    def _export_worker(self, yt, playlists, dest):
        """Background thread entry point: always reports completion.

        See _worker for why the try/finally is not optional.
        """
        try:
            self._export_to_csv(yt, playlists, dest)
        except Exception as e:
            self.worker_queue.put(
                ("log", f"✗ Unexpected error while exporting: {type(e).__name__}: {e}")
            )
        finally:
            self.worker_queue.put(("done", None))

    def _export_to_csv(self, yt, playlists, dest):
        """Fetch each playlist's tracks and write a CSV. `yt` is the snapshot
        taken when the job started — see _add_to_playlists."""
        put = self.worker_queue.put
        for pl in playlists:
            try:
                tracks = yt.get_playlist(pl["playlistId"], limit=None).get("tracks", [])
                path = write_playlist_csv(pl["title"], tracks, dest)
                put(("log", f"→ Saved '{pl['title']}' ({len(tracks)} tracks) to {path.name}"))
            except Exception as e:
                put(("log", f"→ Failed to export '{pl['title']}': {e}"))
            put(("step", None))
        put(("log", "--- Export done. ---"))

    def _poll_worker(self):
        # The reschedule lives in a finally: if draining ever raises, dropping
        # out of the after() chain would freeze every future worker's output.
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "step":
                    self.progress.step(1)
                elif kind == "playlists":
                    self._show_playlists(payload)
                elif kind == "account":
                    self.account_label.configure(text=payload)
                elif kind == "connected":
                    self.yt = payload
                    self.account_label.configure(text="Logged in")
                    self.login_button.configure(text="Re-log in…")
                elif kind == "connect_failed":
                    silent, message = payload
                    self.yt = None
                    if not silent:
                        messagebox.showerror(
                            "Cratefill", f"Could not use saved login:\n{message}"
                        )
                elif kind == "done":
                    self._end_work()
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_worker)


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    apply_dark_theme(root)
    enable_dark_title_bar(root)
    CratefillApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
