"""The Tkinter application: window, theme, dialogs and worker orchestration.

Left pane:  songs loaded from a CSV (artist + title columns, optional station
column shown for reference, extras ignored) or from a folder of music files
(folder name + file name). Click a column title to sort.
Right pane: your YouTube Music playlists after logging in.
Select songs + playlists, click Add: each song is searched on YouTube Music
and added to every selected playlist. Results are reported in the Messages pane.
The reverse also works: select playlists and click "Export CSV…" to save each
one as an Artist/Title/Album CSV file.

This module owns presentation and threading only. The matching rules live in
matching.py, local files in storage.py, and every network call in youtube.py.
"""

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import youtube
from .storage import read_songs_csv, read_songs_folder
from .youtube import AUTH_FILE, clean_pasted_headers, migrate_legacy_auth_file, secure_auth_file

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # optional: without it the app works, minus drag and drop
    DND_FILES = TkinterDnD = None

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
            youtube.save_credentials(headers)
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
            yt = youtube.open_session()
        except Exception as e:
            put(("connect_failed", (silent, str(e))))
        else:
            put(("connected", yt))
            youtube.fetch_playlists(yt, put)
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
            youtube.fetch_playlists(yt, self.worker_queue.put)
        finally:
            self.worker_queue.put(("done", None))

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
        put = self.worker_queue.put
        try:
            youtube.add_songs_to_playlists(yt, songs, playlists, put)
        except Exception as e:
            put(("log", f"✗ Unexpected error while adding: {type(e).__name__}: {e}"))
        finally:
            # The refetch is nested so that nothing it does can stop the "done"
            # message: that message is the only thing that re-enables the UI.
            try:
                youtube.fetch_playlists(yt, put)  # counts changed — refetch here,
            finally:                              # still off the UI thread
                put(("done", None))

    def _export_worker(self, yt, playlists, dest):
        """Background thread entry point: always reports completion.

        See _worker for why the try/finally is not optional.
        """
        try:
            youtube.export_playlists_to_csv(yt, playlists, dest, self.worker_queue.put)
        except Exception as e:
            self.worker_queue.put(
                ("log", f"✗ Unexpected error while exporting: {type(e).__name__}: {e}")
            )
        finally:
            self.worker_queue.put(("done", None))

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
