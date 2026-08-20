"""Tests for AmbiguousMatchDialog. Needs a Tk display, so it skips without one."""

import pytest

tk = pytest.importorskip("tkinter")

from cratefill import policy                     # noqa: E402
from cratefill.app import AmbiguousMatchDialog, apply_dark_theme   # noqa: E402
from cratefill.matching import MatchDecision     # noqa: E402


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:                   # headless CI, no $DISPLAY
        pytest.skip(f"no display: {exc}")
    r.withdraw()
    apply_dark_theme(r)
    yield r
    r.destroy()


@pytest.fixture
def decision():
    return MatchDecision(
        "ambiguous",
        candidate={"videoId": "v1", "title": "Wonderwall (Deluxe)",
                   "artists": [{"name": "Oasis"}]},
        reasons=["another candidate scores almost the same"],
    )


def open_dialog(root, decision):
    dialog = AmbiguousMatchDialog(root, "Oasis", "Wonderwall", decision)
    root.update()
    return dialog


def test_shows_the_request_the_proposal_and_the_reason(root, decision):
    dialog = open_dialog(root, decision)
    shown = " | ".join(
        str(w.cget("text")) for w in dialog.winfo_children()[0].winfo_children()
        if w.winfo_class() == "TLabel"
    )
    assert "Oasis — Wonderwall" in shown          # requested
    assert "Wonderwall (Deluxe)" in shown         # proposed
    assert "almost the same" in shown             # reason
    dialog.destroy()


def test_add_reports_add(root, decision):
    dialog = open_dialog(root, decision)
    dialog._choose(policy.ADD)
    assert (dialog.action, dialog.remember) == ("add", False)


def test_skip_reports_skip(root, decision):
    dialog = open_dialog(root, decision)
    dialog._choose(policy.SKIP)
    assert (dialog.action, dialog.remember) == ("skip", False)


def test_the_checkbox_is_reported(root, decision):
    dialog = open_dialog(root, decision)
    dialog.remember_var.set(True)
    dialog._choose(policy.ADD)
    assert (dialog.action, dialog.remember) == ("add", True)


def test_escape_cancels(root, decision):
    """No action means "cancel the whole import" to the caller, so the binding
    must leave `action` as None rather than defaulting to skip."""
    dialog = open_dialog(root, decision)
    dialog.event_generate("<Escape>")
    root.update()
    assert dialog.action is None


def test_closing_the_window_cancels(root, decision):
    """Same for the window-manager close button, which plain destroy()s it."""
    dialog = open_dialog(root, decision)
    dialog.destroy()
    assert dialog.action is None


def test_defaults_before_any_choice(root, decision):
    dialog = open_dialog(root, decision)
    assert dialog.action is None and dialog.remember is False
    dialog.destroy()
