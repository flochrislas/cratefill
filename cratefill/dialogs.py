"""The application's dialogs: logging in, and reviewing an uncertain match.

Each is a modal Toplevel that reports its outcome through attributes the caller
reads after wait_window() — no callbacks into the main window, which is what
keeps them independent of it. Like the window, they never call YouTube Music
on the UI thread: LoginDialog validates on its own worker thread.
"""

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from urllib.parse import quote

from . import policy, youtube
from .theme import BG, DARK_TEXT_STYLE, FG_DIM, READY, enable_dark_title_bar
from .youtube import AUTH_FILE, clean_pasted_headers

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


def candidate_meta(result):
    """One-line album/duration/year/explicit summary for a search result.

    Every piece is optional — the ytmusicapi search shape is not guaranteed and
    older or non-album tracks routinely miss `album`, `duration` or `year`. Joined
    with " · " so the row stays readable even when only one field is present, and
    returns "" when there is nothing to say (the caller then skips the label
    entirely). This is what makes the dialog able to tell apart two candidates
    that share exact artist + title — the case the reason line alone can't
    disambiguate.
    """
    if not isinstance(result, dict):
        return ""
    parts = []
    album = result.get("album") if isinstance(result.get("album"), dict) else {}
    if album.get("name"):
        parts.append(str(album["name"]))
    if result.get("duration"):
        parts.append(str(result["duration"]))
    if result.get("year"):
        parts.append(str(result["year"]))
    if result.get("isExplicit"):
        parts.append("E")     # matches YouTube Music's own explicit badge
    return " · ".join(parts)


class AmbiguousMatchDialog(tk.Toplevel):
    """Asks what to do about one match that isn't certain.

    Sets `action` to "add" or "skip", or leaves it None if the user dismissed the
    window — which the caller treats as "cancel the whole import", so no playlist
    is touched. `chosen` is the list of candidates to add: the top proposal by
    default, or whichever ones the user ticked — several rivals can be worth
    keeping. `remember` reports whether the choice should become the policy.
    """

    def __init__(self, parent, artist, title, decision):
        super().__init__(parent)
        self.title("Weak match" if decision.status == "weak" else "Ambiguous match")
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        enable_dark_title_bar(self)
        self.action = None
        self.remember = False
        self.choices = decision.choices
        self.chosen = [decision.candidate] if decision.candidate else []

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        self._build_request_summary(body, artist, title, decision)
        # The actions are packed (to the bottom) *before* the list, so they keep
        # their space however many candidates there are — the list is what
        # scrolls. Every version of a song is listed, which can be ten.
        self._build_actions(body, decision)
        self._build_candidate_list(body)
        self.add_button.focus_set()
        self.bind("<Escape>", lambda _e: self.destroy())  # dismiss = cancel the import
        self._select()

    def _build_request_summary(self, body, artist, title, decision):
        """What was asked for, and why we're asking at all."""
        ttk.Label(body, text="Requested:", foreground=FG_DIM).pack(anchor="w")
        ttk.Label(body, text=f"{artist} — {title}", wraplength=520).pack(
            anchor="w", padx=(12, 0), pady=(0, 8)
        )
        # The decision-level reason ("2 versions of this song found"), as
        # opposed to each candidate's own shortfalls.
        ttk.Label(body, text="Reason:", foreground=FG_DIM).pack(anchor="w")
        ttk.Label(body, text=decision.reason, wraplength=520, justify="left").pack(
            anchor="w", padx=(12, 0), pady=(0, 8)
        )
        if decision.status == "weak":
            ttk.Label(
                body,
                text="Weak match — asking whatever your policy says.",
                foreground=READY,
                wraplength=520,
            ).pack(anchor="w", pady=(0, 8))

    def _build_actions(self, body, decision):
        """Skip / Add, and above them the "remember" checkbox (hidden for weak
        matches, which can't be automated anyway). Packed to the bottom."""
        buttons = ttk.Frame(body)
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        self.add_button = ttk.Button(buttons, text="Add",
                                     command=lambda: self._choose(policy.ADD))
        self.add_button.pack(side="right")
        ttk.Button(buttons, text="Skip", command=lambda: self._choose(policy.SKIP)).pack(
            side="right", padx=(0, 8)
        )
        self.remember_var = tk.BooleanVar(value=False)
        self.remember_check = ttk.Checkbutton(
            body,
            text="Use this choice for future ambiguous matches",
            variable=self.remember_var,
        )
        if decision.status != "weak":
            self.remember_check.pack(side="bottom", anchor="w", pady=(4, 0))

    def _build_candidate_list(self, body):
        """The proposal plus its rivals, one ticked row each, in a list that
        scrolls when it doesn't fit (_fit_list). The proposal starts ticked."""
        # The top-ranked candidate is not always the one the user wants, and
        # the reason text says as much.
        caption = "Proposed:" if len(self.choices) == 1 else "Proposed (tick any to add):"
        ttk.Label(body, text=caption, foreground=FG_DIM).pack(anchor="w")
        list_area = ttk.Frame(body)
        list_area.pack(fill="both", expand=True, padx=(12, 0))
        canvas = tk.Canvas(list_area, bg=BG, highlightthickness=0, borderwidth=0,
                           yscrollincrement=20)
        rows = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=rows, anchor="nw")
        self.choice_vars = [tk.BooleanVar(value=index == 0) for index in range(len(self.choices))]
        for candidate, var in zip(self.choices, self.choice_vars):
            self._build_candidate_row(rows, candidate, var)
        self._fit_list(canvas, rows)

    def _build_candidate_row(self, rows, candidate, var):
        row = ttk.Frame(rows)
        row.pack(fill="x", anchor="w")
        # Checkbox + "Open" share the top line so the button lines up with the
        # score, which is what the user's eye tracks along.
        head = ttk.Frame(row)
        head.pack(fill="x", anchor="w")
        ttk.Checkbutton(
            head,
            text=f"{candidate.label}   ({candidate.overall_score:.2f})",
            variable=var,
            command=self._select,
            style="Choice.TCheckbutton",  # wrapped; see theme.apply_dark_theme
        ).pack(side="left", anchor="w")
        # Only offer "Open" when there's actually something to open: a result
        # without a videoId can't be played, and can't be added either.
        if candidate.video_id:
            ttk.Button(
                head,
                text="Open ▶",
                width=8,
                command=lambda vid=candidate.video_id: self._open(vid),
            ).pack(side="right")
        # Album · duration · year · E — the fields that let the user tell
        # apart candidates whose artist and title are identical (reissues,
        # compilations, clean vs explicit). Skipped entirely when the result
        # carries none of them, rather than showing an empty line.
        meta = candidate_meta(candidate.result)
        if meta:
            ttk.Label(row, text=meta, foreground=FG_DIM, wraplength=480).pack(
                anchor="w", padx=(24, 0)
            )
        detail = candidate.reason or "matches exactly"
        ttk.Label(row, text=detail, foreground=FG_DIM, wraplength=480).pack(
            anchor="w", padx=(24, 0), pady=(0, 6)
        )

    def _fit_list(self, canvas, rows):
        """Size the candidate list to its content, or to what the screen has
        room for — with a scrollbar and the mouse wheel — when it doesn't fit."""
        self.update_idletasks()  # the canvas isn't packed yet: this measures the rest
        width, height = rows.winfo_reqwidth(), rows.winfo_reqheight()
        # Leave room for the title bar and a taskbar; the rest of the dialog
        # (request, reason, buttons) keeps its natural height.
        room = max(self.winfo_screenheight() - 120 - self.winfo_reqheight(), 120)
        canvas.configure(width=width, height=min(height, room),
                         scrollregion=(0, 0, width, height))
        if height > room:
            bar = ttk.Scrollbar(canvas.master, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            # Bound on the dialog, whose tag every child carries, so the wheel
            # works wherever the pointer is. Windows/macOS send <MouseWheel>,
            # X11 sends buttons 4 and 5.
            self.bind("<MouseWheel>",
                      lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            self.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"))
            self.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"))
        canvas.pack(side="left", fill="both", expand=True)

    def _select(self):
        """Track the ticked candidates. Add needs at least one — with none ticked
        the only honest answers are Skip or cancelling."""
        self.chosen = [c for c, var in zip(self.choices, self.choice_vars) if var.get()]
        self.add_button.state(["!disabled"] if self.chosen else ["disabled"])

    def _open(self, video_id):
        """Open the candidate on music.youtube.com so the user can hear it.

        The dialog stays up: this is a preview aid for the pending decision, not
        an action of its own. Failure is caught because a missing default browser
        is recoverable — dying here would drop the whole ambiguous match — but it
        is *told* to the user rather than swallowed: a click that does nothing at
        all is indistinguishable from a broken button.

        The id is quoted even though it comes from the API: it is interpolated
        into a URL handed to the OS, and "trusted input" is a bad habit there.
        """
        url = f"https://music.youtube.com/watch?v={quote(str(video_id), safe='')}"
        try:
            webbrowser.open(url)
        except Exception as e:      # noqa: BLE001 — a failed preview must not lose the match
            messagebox.showwarning(
                "Cratefill",
                f"Could not open a browser to preview this track.\n\n{url}\n\nDetails: {e}",
                parent=self,
            )

    def _choose(self, action):
        self._select()
        self.action = action
        self.remember = bool(self.remember_var.get())
        self.destroy()


class AddOrReplaceDialog(tk.Toplevel):
    """Asks what newly chosen files should do to a song list that isn't empty.

    Sets `choice` to "add" (combine them with the list) or "new" (replace it),
    or leaves it None if the window was dismissed — the caller then loads
    nothing. "Add" is the default: it can't lose anything.
    """

    ADD, NEW = "add", "new"

    def __init__(self, parent, count, listed, how="dropped"):
        """`count` files/folders arriving, `listed` songs already there; `how`
        they arrived ("dropped", "selected") for the question's wording."""
        super().__init__(parent)
        self.title("Add to the song list?")
        self.configure(bg=BG)
        self.transient(parent)
        self.resizable(False, False)
        self.grab_set()
        enable_dark_title_bar(self)
        self.choice = None

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        items = "1 item" if count == 1 else f"{count} items"
        ttk.Label(
            body,
            text=f"The song list already has {listed} song(s).\n"
                 f"What should the {items} you {how} do?",
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            body,
            text="Songs already in the list are not added twice.",
            foreground=FG_DIM,
        ).pack(anchor="w", pady=(4, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        self.add_button = ttk.Button(buttons, text="Add and combine to the list",
                                     command=lambda: self._choose(self.ADD))
        self.add_button.pack(side="right")
        self.new_button = ttk.Button(buttons, text="Make a new list",
                                     command=lambda: self._choose(self.NEW))
        self.new_button.pack(side="right", padx=(0, 8))
        self.add_button.focus_set()
        self.bind("<Return>", self._press_focused)
        self.bind("<Escape>", lambda _e: self.destroy())  # dismiss = load nothing

    def _press_focused(self, _event=None):
        """Enter presses the focused button: ttk buttons only answer the space
        bar, and a blanket "Enter = Add" ignored a Tab to "Make a new list".
        Add has focus when the dialog opens, so Enter alone still adds."""
        focused = self.focus_get()
        (focused if focused in (self.add_button, self.new_button) else self.add_button).invoke()

    def _choose(self, choice):
        self.choice = choice
        self.destroy()
