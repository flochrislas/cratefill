"""UI-level tests that construct real widgets — the review dialog and the parts
of app startup that touch them. Needs a Tk display, so it skips without one."""

import pytest

tk = pytest.importorskip("tkinter")

from cratefill import policy                     # noqa: E402
from cratefill.app import AmbiguousMatchDialog, apply_dark_theme, candidate_meta   # noqa: E402
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


def candidate(vid, title, artist="Oasis", score=0.9, reasons=(), extras=None):
    result = {"videoId": vid, "title": title, "artists": [{"name": artist}]}
    if extras:
        result.update(extras)
    return Candidate(result, title_score=score, artist_score=score, overall_score=score,
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


class TestStartupPolicyMigration:
    """Exercises CratefillApp's real startup path: the migration runs before
    anything else, so getting it wrong means adding unreviewed all session."""

    def launch(self, root, monkeypatch, migrate_result):
        from cratefill.app import CratefillApp
        monkeypatch.setattr(policy, "migrate_settings", lambda: migrate_result)
        monkeypatch.setattr(policy, "load_policy", lambda: "add")
        monkeypatch.setattr(policy, "save_policy", lambda value: True)
        app = CratefillApp(root)
        root.update()
        return app

    def test_a_successful_reset_takes_effect(self, root, monkeypatch):
        app = self.launch(root, monkeypatch, (True, True))
        assert app.ambiguous_policy == "ask"
        assert app.policy_combo.get() == "Always ask"

    def test_a_reset_that_could_not_be_saved_still_takes_effect(self, root, monkeypatch):
        """The bug this guards: the app announced the reset, then re-read the
        unchanged file and kept "add" in memory — adding silently all session."""
        app = self.launch(root, monkeypatch, (True, False))
        assert app.ambiguous_policy == "ask", "must not keep adding unreviewed"
        assert app.policy_combo.get() == "Always ask"
        assert any("Could not save" in line
                   for line in app.log_text.get("1.0", "end").splitlines())

    def test_nothing_to_migrate_leaves_the_saved_policy_alone(self, root, monkeypatch):
        app = self.launch(root, monkeypatch, (False, True))
        assert app.ambiguous_policy == "add"
        assert app.policy_combo.get() == "Always add"


class TestCandidateMeta:
    """The per-candidate metadata line is the whole reason the dialog can now
    tell apart two results that share exact artist and title (the reissue vs
    original case). Every field is optional in the ytmusicapi response, so every
    combination has to degrade gracefully."""

    def result(self, **fields):
        base = {"videoId": "v1", "title": "t", "artists": [{"name": "a"}]}
        base.update(fields)
        return base

    def test_all_four_fields_when_all_are_present(self):
        assert candidate_meta(self.result(
            album={"name": "Tiki"}, duration="4:07", year=2003, isExplicit=True,
        )) == "Tiki · 4:07 · 2003 · E"

    def test_explicit_false_produces_no_badge(self):
        """Only tracks flagged explicit get the badge — the whole point is to
        pick them out from clean versions, so a false shouldn't be shown."""
        assert "E" not in candidate_meta(self.result(
            album={"name": "Tiki"}, duration="4:07", isExplicit=False,
        ))

    def test_missing_album_does_not_break_the_rest(self):
        assert candidate_meta(self.result(duration="4:07")) == "4:07"

    def test_missing_everything_returns_empty(self):
        """The caller checks for this and skips the label entirely — an empty
        line under the radio would just look like a UI bug."""
        assert candidate_meta(self.result()) == ""

    def test_year_shows_when_populated(self):
        assert "2003" in candidate_meta(self.result(year=2003))

    def test_survives_a_null_album_shape(self):
        """ytmusicapi is unofficial; `album` has been seen as None or missing.
        The helper has to eat that without an AttributeError."""
        assert candidate_meta(self.result(album=None, duration="3:00")) == "3:00"

    def test_survives_a_non_dict_result(self):
        assert candidate_meta(None) == ""
        assert candidate_meta("not a result") == ""


class TestCandidateMetadataInDialog:
    """The dialog has to actually render what candidate_meta produces — a helper
    that works but isn't wired in wouldn't help the user pick anything."""

    def decision_with_metadata(self):
        return MatchDecision(
            "ambiguous",
            candidate=candidate("v1", "Manyaka O Brazil", artist="Richard Bona",
                                score=0.78, extras={
                                    "album": {"name": "TIKI"}, "duration": "4:08",
                                }),
            reasons=["another candidate scores almost the same"],
            alternatives=[
                candidate("v2", "Manyaka O Brazil", artist="Richard Bona", score=0.78,
                          extras={"album": {"name": "Tiki"}, "duration": "4:07"}),
                candidate("v3", "Manyaka O Brazil", artist="Richard Bona", score=0.78,
                          extras={"album": {"name": "This Is Richard Bona"},
                                  "duration": "4:07"}),
            ],
        )

    def test_each_candidate_shows_its_own_album_and_duration(self, root):
        """This is the real user-facing win: three otherwise-identical rows are
        now distinguishable at a glance."""
        dialog = open_dialog(root, self.decision_with_metadata())
        shown = " | ".join(shown_text(dialog))
        assert "TIKI · 4:08" in shown
        assert "Tiki · 4:07" in shown
        assert "This Is Richard Bona · 4:07" in shown
        dialog.destroy()

    def test_candidate_without_metadata_still_renders(self, root, decision):
        """The Wonderwall fixtures carry no album/duration — the row must still
        show, with the reason and radio intact, just without the meta line."""
        dialog = open_dialog(root, decision)
        shown = " | ".join(shown_text(dialog))
        assert "Wonderwall (Deluxe)" in shown
        assert "almost the same" in shown             # reason still there
        dialog.destroy()


def _find_widgets(widget, cls):
    """Every descendant widget of the given ttk class name."""
    out = []
    for child in widget.winfo_children():
        if child.winfo_class() == cls:
            out.append(child)
        out.extend(_find_widgets(child, cls))
    return out


class TestOpenButton:
    """Clicking "Open" opens the candidate in music.youtube.com so the user can
    hear it before deciding — the one field a dialog can't summarise is what the
    track actually sounds like."""

    def test_open_button_appears_per_candidate(self, root, decision_with_alternatives):
        dialog = open_dialog(root, decision_with_alternatives)
        buttons = [b for b in _find_widgets(dialog, "TButton")
                   if str(b.cget("text")).startswith("Open")]
        # One Open per candidate, plus Skip and Add at the bottom — the two
        # bottom actions are excluded by the "Open" text filter.
        assert len(buttons) == len(decision_with_alternatives.choices)
        dialog.destroy()

    def test_open_button_hidden_when_result_has_no_video_id(self, root):
        """A result without a videoId can't be added *or* played, so offering
        the button would be a lie."""
        no_vid = Candidate({"videoId": None, "title": "t", "artists": [{"name": "a"}]},
                           title_score=0.7, artist_score=0.7, overall_score=0.7,
                           relation="same", reasons=["thin"])
        d = MatchDecision("weak", candidate=no_vid, reasons=["thin"])
        dialog = open_dialog(root, d)
        assert not [b for b in _find_widgets(dialog, "TButton")
                    if str(b.cget("text")).startswith("Open")]
        dialog.destroy()

    def test_clicking_open_calls_the_browser(self, root, monkeypatch, decision):
        """Verifies the URL shape (music.youtube.com/watch?v=<vid>) and that
        clicking one candidate's Open opens *that* candidate, not the winner."""
        opened = []
        monkeypatch.setattr("cratefill.app.webbrowser.open",
                            lambda url: opened.append(url) or True)
        dialog = open_dialog(root, decision)
        dialog._open("v1")
        assert opened == ["https://music.youtube.com/watch?v=v1"]
        dialog.destroy()

    def test_open_swallows_browser_errors(self, root, monkeypatch, decision):
        """A broken default browser is a recoverable annoyance, not a reason to
        drop the whole pending match."""
        def boom(_url):
            raise OSError("no browser configured")
        monkeypatch.setattr("cratefill.app.webbrowser.open", boom)
        dialog = open_dialog(root, decision)
        dialog._open("v1")     # must not raise
        assert dialog.action is None
        dialog.destroy()
