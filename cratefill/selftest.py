"""Check that a build has everything it needs, without a GUI or a login.

Exists because the two ways a frozen build breaks are both invisible: a
PyInstaller exe built `--windowed` shows *nothing* when a module is missing — it
just fails to start — and both known failures are import-time, so "it opened" is
the only signal you'd otherwise get.

    py -m cratefill --selftest        # from source
    Cratefill.exe --selftest          # from a build (console build to see output)

Exit code 0 if every required check passed, 1 otherwise, so this is usable from
a script or CI. Optional pieces (drag and drop, a display) are reported but never
fail the run.
"""

import sys

REQUIRED, OPTIONAL = "required", "optional"


def _check_rapidfuzz():
    """The fuzzy scorer, whose compiled backends load at import time."""
    from .matching import score_text

    score = score_text("hello world there", "hello world here")
    if not 0.0 < score < 1.0:
        raise AssertionError(f"expected a partial score, got {score!r}")
    return f"scoring works ({score:.2f})"


def _check_matching():
    """The whole pipeline, end to end, on data that needs no network."""
    from .matching import choose_match

    def result(title, artist):
        return [{"videoId": "v1", "title": title, "artists": [{"name": artist}]}]

    exact = choose_match("Phoenix", "Lisztomania", result("Lisztomania", "Phoenix"))
    unrelated = choose_match("Cher", "One", result("Someone", "Cherub"))
    if exact.status != "high":
        raise AssertionError(f"an exact match came back {exact.status!r}")
    if unrelated.status != "rejected":
        raise AssertionError(f"an unrelated result came back {unrelated.status!r}")
    return "exact match → high, unrelated → rejected"


def _check_storage():
    """Where credentials and settings will go. Platform-specific, so worth
    printing on the machine you actually ship from."""
    import os

    from .storage import user_data_dir

    path = user_data_dir()
    if not path.is_absolute():
        raise AssertionError(f"data dir is not absolute: {path}")
    existing = next((p for p in [path, *path.parents] if p.exists()), None)
    if existing is None or not os.access(existing, os.W_OK):
        raise AssertionError(f"cannot write anywhere under {path}")
    return str(path)


def _check_policy():
    """Reading the settings file — also proves storage.read_json works."""
    from .policy import POLICIES, load_policy

    value = load_policy()
    if value not in POLICIES:
        raise AssertionError(f"load_policy() returned {value!r}")
    return f"settings readable (policy: {value})"


def _check_ytmusicapi():
    """Constructing a client needs no auth and no network, but it *does* load
    ytmusicapi's gettext catalogues — the exact thing a frozen build drops
    without --collect-all ytmusicapi, and which then fails with the misleading
    "No translation file found for domain: 'base'" on the first real call."""
    from ytmusicapi import YTMusic

    YTMusic(language="en")
    return "imports and loads its locales"


def _check_tkinter():
    """Building real widgets. A missing display is not a build problem, so it is
    reported as skipped rather than failed."""
    import tkinter as tk  # noqa: PLC0415 — deliberately local; see CLAUDE.md

    from .app import CratefillApp, apply_dark_theme

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        return f"skipped — no display ({exc})"
    try:
        root.withdraw()
        apply_dark_theme(root)
        CratefillApp(root)
        root.update()
    finally:
        root.destroy()
    return "widgets build"


def _check_dragdrop():
    from . import app

    if app.TkinterDnD is None:
        raise AssertionError("not bundled — the app works, minus drag and drop")
    return "available"


CHECKS = (
    ("rapidfuzz", REQUIRED, _check_rapidfuzz),
    ("matching", REQUIRED, _check_matching),
    ("storage", REQUIRED, _check_storage),
    ("settings", REQUIRED, _check_policy),
    ("ytmusicapi", REQUIRED, _check_ytmusicapi),
    ("tkinter", REQUIRED, _check_tkinter),
    ("tkinterdnd2", OPTIONAL, _check_dragdrop),
)


def run(out=None):
    """Run every check, print a report, and return True if the build is usable."""
    out = out or sys.stdout
    frozen = " (frozen)" if getattr(sys, "frozen", False) else ""
    print(f"Cratefill self-test{frozen} — Python {sys.version.split()[0]} on {sys.platform}",
          file=out)

    failures = 0
    for name, importance, check in CHECKS:
        try:
            detail, mark = check(), "ok"
        except Exception as exc:                      # noqa: BLE001 — a report, not a handler
            detail = f"{type(exc).__name__}: {exc}" if not isinstance(exc, AssertionError) else str(exc)
            mark = "warn" if importance is OPTIONAL else "FAIL"
            failures += importance is REQUIRED
        print(f"  [{mark:>4}] {name:<12} {detail}", file=out)

    print("PASS — this build has everything it needs" if not failures
          else f"FAIL — {failures} required check(s) failed", file=out)
    return not failures


def main():
    """Entry point for `--selftest`. Returns a process exit code."""
    return 0 if run() else 1
