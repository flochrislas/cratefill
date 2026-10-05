"""The main window and the orchestration of its background jobs.

Left pane:  songs loaded from a CSV (artist + title columns, optional station
column shown for reference, extras ignored) or from a folder of music files
(folder name + file name). Click a column title to sort.
Right pane: your YouTube Music playlists after logging in.
Select songs + playlists, click Add: each song is searched on YouTube Music
and added to every selected playlist. Results are reported in the Messages pane.
The reverse also works: select playlists and click "Export CSV…" to save each
one as an Artist/Title/Album CSV file.

This module owns the window and threading. Its dialogs live in dialogs.py and
the dark theme in theme.py; the matching rules in matching.py, local files in
storage.py, and every network call in youtube.py.
"""

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import policy, youtube
from .storage import read_song_sources
from .dialogs import AddOrReplaceDialog, AmbiguousMatchDialog, LoginDialog
from .theme import (
    DARK_LIST_STYLE, DARK_TEXT_STYLE, FG_DIM, FIELD, apply_dark_theme, enable_dark_title_bar,
)
from .youtube import AUTH_FILE, migrate_legacy_auth_file, secure_auth_file

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # optional: without it the app works, minus drag and drop
    DND_FILES = TkinterDnD = None

# Song Treeview columns: (column id, heading label). The station column is
# only displayed when the loaded CSV actually has station values.
SONG_COLUMNS = (("artist", "Artist"), ("title", "Song"), ("station", "Station"))

# Shown in the Messages pane at startup, so the app never opens on a blank window.
HELP_TEXT = """\
How to use:

1. Load songs from CSV files or a folder (several CSVs are combined).
2. Log into YouTube Music and select a playlist.
3. Select the songs you want to add to this playlist.
4. Click the big "Add selected songs to selected playlist(s)" button.

You can also export the list of songs from a selected playlist into a CSV file."""


class CratefillApp:
    def __init__(self, root, startup=True):
        """Build the window. With `startup` (the default) also do the launch
        work that touches the outside world: migrate settings and an old
        session file, and connect if a session is saved. Pass startup=False to
        get just the interface — the self-check, previews and widget tests must
        not rewrite settings.json or open a YouTube Music session."""
        self.root = root
        self.root.title("Cratefill — CSV to YouTube Music")
        self.root.geometry("1080x680")

        self.yt = None
        self.songs = []  # list of storage.Song
        self.source_names = []  # the files and folders self.songs came from, for the label
        self.drop_pending = False  # a drop is being asked about; see _on_drop
        self.song_sort = (None, False)  # (column id, descending?)
        self.playlists = []  # list of dicts from get_library_playlists
        self.worker_queue = queue.Queue()
        self.working = False
        # Match decisions waiting to be reviewed between the two add phases.
        self.pending_review = None
        self.ambiguous_policy = policy.load_policy()

        self._build_ui()
        self.show_help()
        self.root.after(100, self._poll_worker)
        if startup:
            self._start_up()

    def _start_up(self):
        """Launch work with side effects; see __init__."""
        reset, saved = policy.migrate_settings()
        if reset:
            # Forced in memory even when the file couldn't be written: the whole
            # point is not to keep adding unreviewed under the old agreement.
            # Re-reading the file here would hand back the stale "add".
            self.set_ambiguous_policy(policy.ASK, persist=saved)
            self.log(
                "'Always add' has been reset to 'Always ask': this version offers "
                "matches it used to refuse, so re-select it if you still want it."
                + ("" if saved else f"\n    (Could not save that to {policy.SETTINGS_FILE} —"
                                    " it will be reset again next launch.)")
            )
        if migrate_legacy_auth_file():
            self.log(f"Moved your saved session to {AUTH_FILE.parent}")
        secure_auth_file()  # tighten a file written by an older version
        if AUTH_FILE.exists():
            self._connect(silent=True)

    # ---------- UI construction ----------

    def _build_ui(self):
        """The window: songs (left) and YouTube Music (right) side by side, then
        the Process bar and the Messages pane across the bottom."""
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(main, orient="horizontal")
        panes.pack(fill="both", expand=True)
        panes.add(self._build_songs_pane(panes), weight=3)
        panes.add(self._build_youtube_pane(panes), weight=2)
        self._build_process_bar(main)
        self._build_messages(main)

        # Everything that talks to YouTube Music, disabled for the duration of
        # a job by _start_work — see there for why Log in and Refresh count.
        self.busy_controls = (
            self.add_button, self.export_button, self.login_button, self.refresh_button,
            self.liked_only_check,  # read when Export starts; changing it mid-run would do nothing
        )

    def _build_songs_pane(self, parent):
        """Load buttons, the song list (sortable, droppable) and its empty hint."""
        left = ttk.LabelFrame(parent, text="Songs list", padding=4)
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

        tree_accepts_drops = self._register_drop_target(self.song_tree)

        # An empty song list looks broken rather than ready, and drag-and-drop is
        # invisible until you know it's there. This says so, in the space it's
        # talking about, and gets out of the way the moment anything loads.
        # Placed over the tree rather than inserted as a row: a placeholder row
        # would land in self.songs' index mapping and in "Select all".
        self.empty_hint = ttk.Label(
            self.song_tree, justify="center", foreground=FG_DIM, background=FIELD,
        )
        # The hint covers the middle of the area it points at, so it has to accept
        # drops itself — and only advertise dropping if *both* targets took. A
        # registered tree behind an unregistered label would invite drops onto a
        # dead zone in the very spot the text says to aim for.
        hint_accepts_drops = self._register_drop_target(self.empty_hint)
        self.empty_hint.configure(
            text="Drag CSV files or music folders here\n(several at once are combined)\n\n"
                 "or use Load CSV… / Load folder… above"
            if tree_accepts_drops and hint_accepts_drops
            else "Use Load CSV… or Load folder… above to get started",
        )
        self._refresh_empty_hint()
        return left

    def _build_youtube_pane(self, parent):
        """Account buttons, the liked-only export option and the playlist list."""
        right = ttk.LabelFrame(parent, text="YouTube Music", padding=4)
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

        self.liked_only_var = tk.BooleanVar(value=False)
        self.liked_only_check = ttk.Checkbutton(
            right, text="Export only the songs I liked", variable=self.liked_only_var
        )
        self.liked_only_check.pack(anchor="w", pady=(0, 6))

        self.playlist_list = tk.Listbox(
            right, selectmode="extended", exportselection=False, **DARK_LIST_STYLE
        )
        self.playlist_list.bind("<<ListboxSelect>>", lambda _e: self.refresh_add_button())
        playlist_scroll = ttk.Scrollbar(right, orient="vertical", command=self.playlist_list.yview)
        self.playlist_list.configure(yscrollcommand=playlist_scroll.set)
        self.playlist_list.pack(side="left", fill="both", expand=True)
        playlist_scroll.pack(side="right", fill="y")
        return right

    def _build_process_bar(self, parent):
        """The ambiguous-match policy, the Add button and the progress bar."""
        bottom = ttk.LabelFrame(parent, text="Process", padding=4)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, text="On ambiguous match:").pack(side="left", padx=(0, 6))
        self.policy_combo = ttk.Combobox(
            bottom,
            state="readonly",
            width=12,
            values=[policy.POLICY_LABELS[p] for p in policy.POLICIES],
        )
        self.policy_combo.set(policy.POLICY_LABELS[self.ambiguous_policy])
        self.policy_combo.bind("<<ComboboxSelected>>", self._on_policy_selected)
        self.policy_combo.pack(side="left", padx=(0, 10))
        self.add_button = ttk.Button(
            bottom, text="Add selected songs to selected playlist(s)", command=self.add_songs
        )
        self.add_button.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

    def _build_messages(self, parent):
        """The Messages pane: every job reports here."""
        log_frame = ttk.LabelFrame(parent, text="Messages", padding=4)
        log_frame.pack(fill="both", pady=(8, 0))
        self.log_text = tk.Text(
            log_frame, height=9, state="disabled", wrap="word", **DARK_TEXT_STYLE
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    # ---------- Ambiguous-match policy ----------

    def _on_policy_selected(self, _event=None):
        chosen = policy.LABEL_POLICIES[self.policy_combo.get()]
        # "Always add" is a standing instruction to skip review, so spell out
        # what it now accepts before taking it. Weak matches still always ask.
        if chosen == policy.ADD and not messagebox.askokcancel(
            "Cratefill — Always add",
            "Uncertain matches will be added without asking.\n\n"
            "That includes a different recording (a live version, a remix) and a "
            "song credited to a different artist, as long as the title clearly "
            "matches. Only the weakest matches will still ask.\n\n"
            "Add uncertain matches automatically?",
        ):
            self.policy_combo.set(policy.POLICY_LABELS[self.ambiguous_policy])
            return
        self.set_ambiguous_policy(chosen)

    def set_ambiguous_policy(self, value, persist=True):
        """Adopt a policy and persist it immediately, keeping the dropdown in sync.

        Called by the dropdown, by "use this choice for future ambiguous matches"
        in the review dialog, and by the startup migration — they must not drift
        apart. `persist=False` skips the write for a caller that has already
        written (or already failed to) and reports that itself.
        """
        self.ambiguous_policy = value
        self.policy_combo.set(policy.POLICY_LABELS[value])
        if persist and not policy.save_policy(value):
            self.log(f"Could not save your preference to {policy.SETTINGS_FILE}")

    def _register_drop_target(self, widget):
        """Let `widget` accept dropped files. True if drag-and-drop really works.

        False when tkinterdnd2 isn't installed, or when the root is a plain
        tk.Tk() rather than a TkinterDnD.Tk() — which is the case in tests and
        the screenshot preview. The return value decides whether the UI is
        allowed to *advertise* dropping.
        """
        if not DND_FILES:
            return False
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except tk.TclError:
            return False
        return True

    def _refresh_empty_hint(self):
        """Show the drop hint only while the song list is empty."""
        if self.songs:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.45, anchor="center")

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

    # ---------- Left pane: loading songs ----------

    def load_csv(self):
        paths = filedialog.askopenfilenames(
            title="Open songs CSV (select several to combine them)",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if paths:
            self._load_asking(paths, "selected")

    def load_folder(self):
        path = filedialog.askdirectory(title="Open a folder of music files")
        if path:
            self._load_asking([path], "selected")

    def _on_drop(self, event):
        """Load everything dropped onto the song list — files and folders.

        The drop is acknowledged at once and handled just after: on Windows the
        drag source (Explorer) waits for this callback to return, so asking
        add-or-replace in here would freeze it until the user answered. Drops
        arriving while one is still pending or being asked about are ignored —
        the dialog's grab doesn't stop drag-and-drop, and they would stack.
        """
        paths = self.song_tree.tk.splitlist(event.data)
        if not paths:
            return
        if self.drop_pending:
            self.log("Answer the open question before dropping more.")
            return
        self.drop_pending = True
        self.root.after_idle(self._finish_drop, paths)

    def _finish_drop(self, paths):
        try:
            self._load_asking(paths, "dropped")
        finally:
            self.drop_pending = False

    def _load_asking(self, paths, how):
        """Load paths the user just picked or dropped. With songs already
        listed, they choose first: combine with the list, or make a new one.
        Dismissing the question loads nothing."""
        choice = self._ask_add_or_replace(len(paths), how) if self.songs \
            else AddOrReplaceDialog.NEW
        if choice is not None:
            self.load_paths(paths, add=choice == AddOrReplaceDialog.ADD)

    def _ask_add_or_replace(self, count, how):
        """AddOrReplaceDialog.ADD / NEW, or None if the user dismissed it."""
        dialog = AddOrReplaceDialog(self.root, count, len(self.songs), how)
        self.root.wait_window(dialog)
        return dialog.choice

    def load_paths(self, paths, add=False):
        """Load these CSV files and music folders, combined in order
        (storage.read_song_sources): as a new list, or with `add` appended to
        the current one, skipping songs it already has and keeping its
        selection.

        A path that gives nothing is skipped and named, in the log and in a
        warning; the rest still load. If nothing loads at all, the current list
        is left alone rather than emptied.
        """
        existing = self.songs if add else []
        loaded = read_song_sources(paths, existing=existing)
        for path, reason in loaded.skipped:
            self.log(f"✗ Skipped {path.name}: {reason}")
        if loaded.skipped:
            listed = "\n".join(f"• {path.name}: {reason}" for path, reason in loaded.skipped)
            kept = "" if loaded.songs else "\n\nThe song list was left unchanged."
            messagebox.showwarning("Cratefill", f"Could not load:\n{listed}{kept}")
        if not loaded.songs:
            if loaded.sources:  # read fine, but every song was already listed
                self.log("Nothing new: every song loaded is already in the list.")
            return

        selected = self.song_tree.selection() if add else ()
        self.songs = existing + loaded.songs
        self.source_names = (self.source_names if add else []) + [
            path.name for path, _count in loaded.sources]
        self.populate_song_tree()
        if selected:  # appended rows leave the old iids pointing where they did
            self.song_tree.selection_set(selected)
            self.refresh_add_button()
        first, count = self.source_names[0], len(self.source_names)
        where = first if count == 1 else f"{first} + {count - 1} more"
        self.csv_label.configure(text=f"{where} — {len(self.songs)} songs")

        merged = f" ({loaded.duplicates} duplicate(s) skipped)" if loaded.duplicates else ""
        verb, total = ("Added", f", {len(self.songs)} in the list") if add else ("Loaded", "")
        sources = loaded.sources
        if len(sources) == 1:
            self.log(f"{verb} {len(loaded.songs)} songs from {sources[0][0]}{merged}{total}")
        else:
            self.log(f"{verb} {len(loaded.songs)} songs from {len(sources)} sources{merged}{total}:")
            for path, n in sources:
                self.log(f"    {path.name}: {n} song(s)")

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
        self._refresh_empty_hint()

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
        self._run_job("connecting", self._connect_job, silent)

    @staticmethod
    def _connect_job(silent, put):
        """Open the saved session, then fetch its playlists."""
        try:
            yt = youtube.open_session()
        except Exception as e:
            put(("connect_failed", (silent, str(e))))
        else:
            put(("connected", yt))
            youtube.fetch_playlists(yt, put)

    def refresh_playlists(self):
        # Never hit the API from here while a worker owns the client. _poll_worker
        # calls _end_work() (clearing self.working) before starting this.
        if self.working:
            return
        if not self.yt:
            self.log("Not logged in — click 'Log in…' first.")
            return
        self._start_work()
        self._run_job("refreshing", youtube.fetch_playlists, self.yt)

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

        self._start_work(maximum=len(selected_songs))
        self.log(
            f"--- Matching {len(selected_songs)} song(s) for "
            f"{len(selected_playlists)} playlist(s) ---"
        )
        # Snapshot the client: the worker must keep using the account it started
        # with, even if self.yt is replaced later.
        self._run_job("matching", self._match_job, self.yt, selected_songs, selected_playlists)

    # ---------- Reviewing matches, then adding ----------

    def _review_and_add(self, yt, evaluated, playlists):
        """Main thread: turn match decisions into an approved list, then add.

        Runs between the two worker phases. Nothing has touched a playlist yet,
        which is what makes cancelling here safe and predictable.
        """
        approvals = policy.Approvals()
        for song, decision in evaluated:
            artist, title = song[0], song[1]
            video_ids = [decision.video_id] if decision.video_id else []
            action = policy.action_for_match(decision, self.ambiguous_policy)
            if action == policy.ASK:
                choice, chosen = self._ask_about_match(artist, title, decision)
                if choice is None:  # dialog dismissed → abandon the whole import
                    self.log("--- Cancelled. No playlist was changed. ---")
                    return
                action = choice
                # The user may tick several candidates for one requested song.
                video_ids = [c.video_id for c in chosen if c.video_id]
            elif decision.status == "ambiguous":
                verb = "added" if action == policy.ADD else "skipped"
                self.log(f"    → ambiguous match {verb} by policy")
            approvals.record(action, video_ids)

        self.log(approvals.summary())
        if not approvals.video_ids:
            self.log("Nothing approved — no playlist was changed.")
            return
        self._start_work(maximum=len(playlists))
        self._run_job("adding", self._add_job, yt, approvals.video_ids, playlists)

    def _ask_about_match(self, artist, title, decision):
        """Show the review dialog.

        Returns (action, chosen candidates): "add"/"skip" and the list of
        candidates the user ticked, or (None, [...]) to cancel the whole import.
        """
        dialog = AmbiguousMatchDialog(self.root, artist, title, decision)
        self.root.wait_window(dialog)
        if dialog.action:
            self.log(f"    → {dialog.action} (your choice)")
            if dialog.action == policy.ADD and dialog.chosen != [decision.candidate]:
                for candidate in dialog.chosen:
                    self.log(f"    → you picked: {candidate.label}")
        if dialog.remember and dialog.action:
            self.set_ambiguous_policy(dialog.action)
            self.log(f"    → remembering '{policy.POLICY_LABELS[dialog.action]}'")
        return dialog.action, dialog.chosen

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
        liked_only = self.liked_only_var.get()
        self._start_work(maximum=len(selected))
        what = "liked songs of " if liked_only else ""
        self.log(f"--- Exporting {what}{len(selected)} playlist(s) to {dest} ---")
        # Snapshot self.yt — see add_songs.
        self._run_job("exporting", self._export_job, self.yt, selected, dest, liked_only)

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

    def _run_job(self, what, job, *args):
        """Run job(*args, put) on a background thread; see _job_body.

        Every YouTube Music call goes through here, off the UI thread. The
        client is passed in as an argument, never read off self.yt inside the
        job, so a job stays bound to the account it started with.
        """
        threading.Thread(target=self._job_body, args=(what, job, *args), daemon=True).start()

    def _job_body(self, what, job, *args):
        """Run a job and always report completion.

        The busy controls stay disabled until ("done", …) arrives, so a job that
        dies on an unexpected error — malformed API data, say — would leave the
        UI unusable until restart. Hence the finally: the thread cannot exit
        without re-enabling the UI, and the error is logged for the user.
        """
        put = self.worker_queue.put
        try:
            job(*args, put)
        except Exception as e:
            put(("log", f"✗ Unexpected error while {what}: {type(e).__name__}: {e}"))
        finally:
            put(("done", None))

    @staticmethod
    def _match_job(yt, songs, playlists, put):
        """Phase one: search and score, never mutate. The decisions are handed
        over for review; nothing is added until that is done."""
        evaluated = youtube.evaluate_songs(yt, songs, put)
        put(("decisions", (yt, evaluated, playlists)))

    @staticmethod
    def _add_job(yt, video_ids, playlists, put):
        """Phase two: add the approved ids, then refetch the playlists — their
        counts changed — even if adding failed."""
        try:
            youtube.add_video_ids_to_playlists(yt, video_ids, playlists, put)
        finally:
            youtube.fetch_playlists(yt, put)  # swallows its own errors

    @staticmethod
    def _export_job(yt, playlists, dest, liked_only, put):
        youtube.export_playlists_to_csv(yt, playlists, dest, put, liked_only=liked_only)

    def _poll_worker(self):
        """Drain the worker queue on the main thread, every 100 ms.

        Each message kind has one handler (_handlers). The reschedule lives in
        a finally: if draining ever raises, dropping out of the after() chain
        would freeze every future job's output.
        """
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                self._handlers[kind](payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_worker)

    @property
    def _handlers(self):
        """Message kind → what the main thread does with its payload."""
        return {
            "log": self.log,
            "step": lambda _payload: self.progress.step(1),
            "playlists": self._show_playlists,
            "account": lambda text: self.account_label.configure(text=text),
            "connected": self._on_connected,
            "connect_failed": self._on_connect_failed,
            "decisions": self._on_decisions,
            "done": self._on_done,
        }

    def _on_connected(self, yt):
        self.yt = yt
        self.account_label.configure(text="Logged in")
        self.login_button.configure(text="Re-log in…")

    def _on_connect_failed(self, payload):
        silent, message = payload
        self.yt = None
        if not silent:
            messagebox.showerror("Cratefill", f"Could not use saved login:\n{message}")

    def _on_decisions(self, review):
        """Stashed rather than acted on: the review has to wait for this job's
        "done", because starting phase two needs _end_work() to have cleared
        self.working first."""
        self.pending_review = review

    def _on_done(self, _payload):
        self._end_work()
        review, self.pending_review = self.pending_review, None
        if review:
            self._review_and_add(*review)


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    apply_dark_theme(root)
    enable_dark_title_bar(root)
    CratefillApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
