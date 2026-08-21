"""Tests for AmbiguousMatchDialog. Needs a Tk display, so it skips without one."""

import pytest

tk = pytest.importorskip("tkinter")

from cratefill import policy                     # noqa: E402
from cratefill.app import AmbiguousMatchDialog, apply_dark_theme   # noqa: E402
from cratefill.matching import Candidate, MatchDecision     # noqa: E402


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


def candidate(vid, title, artist="Oasis", score=0.9, reasons=()):
    return Candidate({"videoId": vid, "title": title, "artists": [{"name": artist}]},
                     title_score=score, artist_score=score, overall_score=score,
                     relation="same", reasons=list(reasons))


@pytest.fixture
def decision():
    return MatchDecision(
        "ambiguous",
        candidate=candidate("v1", "Wonderwall (Deluxe)"),
        reasons=["another candidate scores almost the same"],
    )


@pytest.fixture
def decision_with_alternatives():
    return MatchDecision(
        "ambiguous",
        candidate=candidate("v1", "Wonderwall (Deluxe)", score=0.95),
        reasons=["another candidate scores almost the same"],
        alternatives=[
            candidate("v2", "Wonderwall", score=0.94, reasons=["a close second"]),
            candidate("v3", "Wonderwall (Live)", score=0.80, reasons=["live version"]),
        ],
    )


def open_dialog(root, decision):
    dialog = AmbiguousMatchDialog(root, "Oasis", "Wonderwall", decision)
    root.update()
    return dialog


def shown_text(widget):
    """Every label and radio caption in the dialog, flattened."""
    out = []
    for child in widget.winfo_children():
        if child.winfo_class() in ("TLabel", "TRadiobutton", "TCheckbutton"):
            out.append(str(child.cget("text")))
        out.extend(shown_text(child))
    return out


def test_shows_the_request_the_proposal_and_the_reason(root, decision):
    dialog = open_dialog(root, decision)
    shown = " | ".join(shown_text(dialog))
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


class TestAlternatives:
    """The top-ranked candidate isn't always the one the user wants, and the
    reason text says as much — so the rivals have to be selectable."""

    def test_every_candidate_is_listed(self, root, decision_with_alternatives):
        dialog = open_dialog(root, decision_with_alternatives)
        shown = " | ".join(shown_text(dialog))
        for title in ("Wonderwall (Deluxe)", "Wonderwall", "Wonderwall (Live)"):
            assert title in shown
        assert "a close second" in shown, "each candidate shows its own shortfall"
        dialog.destroy()

    def test_the_winner_is_chosen_by_default(self, root, decision_with_alternatives):
        dialog = open_dialog(root, decision_with_alternatives)
        assert dialog.chosen is decision_with_alternatives.candidate
        dialog._choose(policy.ADD)
        assert dialog.chosen.video_id == "v1"

    def test_picking_an_alternative_changes_what_is_added(self, root,
                                                          decision_with_alternatives):
        dialog = open_dialog(root, decision_with_alternatives)
        dialog.choice_var.set(1)          # the radio list's second entry
        dialog._choose(policy.ADD)
        assert dialog.chosen.video_id == "v2"
        assert dialog.action == "add"

    def test_a_single_candidate_still_works(self, root, decision):
        dialog = open_dialog(root, decision)
        dialog._choose(policy.ADD)
        assert dialog.chosen.video_id == "v1"


class TestWeakMatches:
    def weak(self):
        return MatchDecision("weak", candidate=candidate("v1", "Hello World Goodbye"),
                             reasons=["title similarity 0.33 below 0.90"])

    def test_says_it_will_always_ask(self, root):
        dialog = open_dialog(root, self.weak())
        assert "asking whatever your policy says" in " | ".join(shown_text(dialog))
        dialog.destroy()

    def test_hides_the_remember_checkbox(self, root):
        """Remembering only governs ambiguous matches, so offering it here would
        imply weak ones could be automated too."""
        dialog = open_dialog(root, self.weak())
        assert not dialog.remember_check.winfo_ismapped()
        dialog.destroy()

    def test_titled_distinctly(self, root):
        dialog = open_dialog(root, self.weak())
        assert dialog.title() == "Weak match"
        dialog.destroy()
