"""The dark theme: palette, ttk styles, and a dark title bar on Windows.

Hand-rolled on top of the built-in "clam" theme, the only one that renders the
same on Windows and Linux (platform themes and sv-ttk were tried; see
CLAUDE.md). Pure styling — no application state — so the window and the
dialogs share it without depending on each other.
"""

import sys
from tkinter import ttk

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
    # Combobox needs its field styled explicitly, and its drop-down list is a
    # plain tk Listbox that ttk styles don't reach at all — hence option_add.
    style.configure(
        "TCombobox",
        fieldbackground=FIELD, background=BTN, foreground=FG,
        arrowcolor=FG, bordercolor=BORDER, lightcolor=BTN, darkcolor=BTN,
        selectbackground=FIELD, selectforeground=FG, padding=4,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", FIELD), ("disabled", BG)],
        foreground=[("disabled", FG_DIM)],
        arrowcolor=[("disabled", FG_DIM), ("active", ACCENT_BAR)],
        background=[("active", BTN_ACTIVE)],
    )
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # Indicators: clam's element takes indicator*background*/*foreground*, not the
    # default theme's "indicatorcolor" — set that and it is silently ignored,
    # leaving a light circle that reads as *filled* when it isn't selected.
    for widget in ("TRadiobutton", "TCheckbutton"):
        style.configure(
            widget,
            background=BG, foreground=FG, focuscolor=BORDER,
            indicatorbackground=FIELD, indicatorforeground=ACCENT_BAR,
            upperbordercolor=BORDER, lowerbordercolor=BORDER,
            # Bigger than clam's default, which is a ~6px dot. In the review
            # dialog this is the only thing showing *which* candidate is armed,
            # and it sits at the top of the window while the Add button is at the
            # bottom — at this size it fills the ring, so selected vs not reads as
            # blue vs empty rather than as a speck.
            indicatorsize=16,
        )
        style.map(
            widget,
            background=[("active", BG)],
            foreground=[("disabled", FG_DIM)],
            indicatorbackground=[("disabled", BG), ("pressed", BTN_ACTIVE)],
            indicatorforeground=[("disabled", FG_DIM)],
        )
    # Every label in the review dialog wraps except the candidate checkboxes,
    # which would otherwise let one long "Artist — Title (Live at …)" drag the
    # dialog wider than the screen. ttk.Checkbutton takes no `wraplength`
    # argument, but its label element does through a style.
    style.configure("Choice.TCheckbutton", wraplength=430)

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
