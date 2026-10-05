"""UI-level tests that construct real widgets — the review dialog and the parts
of app startup that touch them. Needs a Tk display, so it skips without one."""

import pytest

tk = pytest.importorskip("tkinter")

from cratefill import policy                     # noqa: E402
from cratefill.dialogs import AmbiguousMatchDialog, candidate_meta   # noqa: E402
from cratefill.theme import apply_dark_theme                          # noqa: E402
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


@pytest.fixture(autouse=True)
def no_real_session(tmp_path, monkeypatch):
    """No test here may use the developer's saved session. The startup tests
    run CratefillApp's real launch path, which connects when browser.json
    exists — a real network call from the suite, on a background thread that
    then garbage-collected other tests' Tk variables off the main thread."""
    import cratefill.app as app_module
    monkeypatch.setattr(app_module, "AUTH_FILE", tmp_path / "no-session.json")
    monkeypatch.setattr(app_module, "migrate_legacy_auth_file", lambda: False)
    monkeypatch.setattr(app_module, "secure_auth_file", lambda *a: None)
    monkeypatch.setattr(app_module.youtube, "open_session", _no_network)


@pytest.fixture(autouse=True)
def no_modal_boxes(monkeypatch):
    """A message box nobody answers blocks forever — on Windows CI, which has
    a real desktop, that hung a release for ten minutes. Here an unexpected
    box fails the test at once; tests that expect one patch it themselves."""
    from tkinter import messagebox

    def unexpected(*args, **kwargs):
        raise AssertionError(f"unexpected message box: {args}")

    for name in ("showinfo", "showwarning", "showerror", "askokcancel", "askyesno"):
        monkeypatch.setattr(messagebox, name, unexpected)


def _no_network():
    raise AssertionError("a test tried to open a YouTube Music session")


@pytest.fixture
def mapped_root():
    """For key-binding tests. Key events go to the focused window, and with no
    window manager (Xvfb) a dialog over a withdrawn root never gets focus — a
    generated key is silently dropped, and a test that only checks "nothing
    was chosen" passes without the binding ever running."""
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    apply_dark_theme(r)
    for _ in range(3):
        r.update()
    yield r
    r.destroy()


def press(root, dialog, key):
    """Deliver a key to a dialog for real: map it, focus it, send the key."""
    for _ in range(3):
        root.update()
    dialog.focus_force()
    root.update()
    dialog.event_generate(key)
    root.update()


def candidate(vid, title, artist="Oasis", score=0.9, reasons=(), extras=None):
    result = {"videoId": vid, "title": title, "artists": [{"name": artist}]}
    if extras:
        result.update(extras)
    return Candidate(result, title_score=score, principal_score=score, artist_score=score, overall_score=score,
                     relation="same", reasons=list(reasons))


@pytest.fixture
def decision():
    return MatchDecision(
        "ambiguous",
        candidate=candidate("v1", "Wonderwall (Deluxe)"),
        reasons=["2 versions of this song found"],
    )


@pytest.fixture
def decision_with_alternatives():
    return MatchDecision(
        "ambiguous",
        candidate=candidate("v1", "Wonderwall (Deluxe)", score=0.95),
        reasons=["2 versions of this song found"],
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
    """Every label and checkbox caption in the dialog, flattened."""
    out = []
    for child in widget.winfo_children():
        if child.winfo_class() in ("TLabel", "TCheckbutton"):
            out.append(str(child.cget("text")))
        out.extend(shown_text(child))
    return out


def test_shows_the_request_the_proposal_and_the_reason(root, decision):
    dialog = open_dialog(root, decision)
    shown = " | ".join(shown_text(dialog))
    assert "Oasis — Wonderwall" in shown          # requested
    assert "Wonderwall (Deluxe)" in shown         # proposed
    assert "2 versions" in shown             # reason
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


def test_escape_cancels(mapped_root, decision):
    """No action means "cancel the whole import" to the caller, so the binding
    must leave `action` as None rather than defaulting to skip."""
    dialog = open_dialog(mapped_root, decision)
    press(mapped_root, dialog, "<Escape>")
    assert not dialog.winfo_exists(), "Escape must close the dialog"
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
        assert dialog.chosen == [decision_with_alternatives.candidate]
        dialog._choose(policy.ADD)
        assert [c.video_id for c in dialog.chosen] == ["v1"]

    def test_picking_an_alternative_changes_what_is_added(self, root,
                                                          decision_with_alternatives):
        dialog = open_dialog(root, decision_with_alternatives)
        dialog.choice_vars[0].set(False)
        dialog.choice_vars[1].set(True)   # the list's second entry
        dialog._choose(policy.ADD)
        assert [c.video_id for c in dialog.chosen] == ["v2"]
        assert dialog.action == "add"

    def test_several_candidates_can_be_ticked(self, root, decision_with_alternatives):
        dialog = open_dialog(root, decision_with_alternatives)
        dialog.choice_vars[2].set(True)
        dialog._choose(policy.ADD)
        assert [c.video_id for c in dialog.chosen] == ["v1", "v3"]

    def test_add_is_disabled_with_nothing_ticked(self, root, decision_with_alternatives):
        dialog = open_dialog(root, decision_with_alternatives)
        dialog.choice_vars[0].set(False)
        dialog._select()
        assert dialog.add_button.instate(["disabled"])
        dialog.choice_vars[1].set(True)
        dialog._select()
        assert not dialog.add_button.instate(["disabled"])
        dialog.destroy()

    def test_a_single_candidate_still_works(self, root, decision):
        dialog = open_dialog(root, decision)
        dialog._choose(policy.ADD)
        assert [c.video_id for c in dialog.chosen] == ["v1"]


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
        line under the checkbox would just look like a UI bug."""
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
            reasons=["2 versions of this song found"],
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
        show, with the reason and checkbox intact, just without the meta line."""
        dialog = open_dialog(root, decision)
        shown = " | ".join(shown_text(dialog))
        assert "Wonderwall (Deluxe)" in shown
        assert "2 versions" in shown             # reason still there
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
                           title_score=0.7, principal_score=0.7, artist_score=0.7, overall_score=0.7,
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
        monkeypatch.setattr("cratefill.dialogs.webbrowser.open",
                            lambda url: opened.append(url) or True)
        dialog = open_dialog(root, decision)
        dialog._open("v1")
        assert opened == ["https://music.youtube.com/watch?v=v1"]
        dialog.destroy()

    def test_open_reports_browser_errors_without_losing_the_match(self, root, monkeypatch,
                                                                  decision):
        """A broken default browser is a recoverable annoyance, not a reason to
        drop the whole pending match — but it is *told* to the user, because a
        click that silently does nothing is indistinguishable from a dead button.
        """
        def boom(_url):
            raise OSError("no browser configured")
        warned = []
        monkeypatch.setattr("cratefill.dialogs.webbrowser.open", boom)
        monkeypatch.setattr("cratefill.dialogs.messagebox.showwarning",
                            lambda *a, **k: warned.append(a[1]))
        dialog = open_dialog(root, decision)
        dialog._open("v1")     # must not raise
        assert dialog.action is None, "the decision is still pending"
        assert warned and "Could not open a browser" in warned[0]
        dialog.destroy()

    @pytest.mark.parametrize("video_id, expected", [
        ("abc123", "abc123"),
        ("a b&c", "a%20b%26c"),            # nothing can slip out of the query value
        ("../evil", "..%2Fevil"),
    ])
    def test_the_video_id_is_url_encoded(self, root, monkeypatch, decision, video_id,
                                         expected):
        """The id comes from an unofficial API and ends up in a URL handed to the
        OS, so it is quoted rather than trusted."""
        opened = []
        monkeypatch.setattr("cratefill.dialogs.webbrowser.open", lambda url: opened.append(url))
        dialog = open_dialog(root, decision)
        dialog._open(video_id)
        assert opened == [f"https://music.youtube.com/watch?v={expected}"]
        dialog.destroy()


class TestEmptyListHint:
    """An empty song list looks broken rather than ready, and drag-and-drop is
    invisible until you know it's there."""

    def app(self, root):
        from cratefill.app import CratefillApp
        app = CratefillApp(root, startup=False)
        root.update()
        return app

    def shown(self, app):
        # winfo_ismapped() is unreliable on a compositor that won't map windows;
        # the geometry manager reflects place()/place_forget() either way.
        return app.empty_hint.winfo_manager() == "place"

    def test_shown_while_the_list_is_empty(self, root):
        assert self.shown(self.app(root)) is True

    def test_hidden_once_songs_load(self, root):
        app = self.app(root)
        app.songs = [("Phoenix", "Lisztomania", "")]
        app.populate_song_tree()
        assert self.shown(app) is False

    def test_returns_when_the_list_is_cleared(self, root):
        app = self.app(root)
        app.songs = [("Phoenix", "Lisztomania", "")]
        app.populate_song_tree()
        app.songs = []
        app.populate_song_tree()
        assert self.shown(app) is True

    def test_it_does_not_become_a_row_in_the_song_list(self, root):
        """A placeholder *row* would land in self.songs' index mapping and in
        "Select all"; this is an overlay precisely to avoid that."""
        app = self.app(root)
        assert app.song_tree.get_children() == ()

    def test_it_only_advertises_dropping_when_dropping_works(self, root):
        """The tests use a plain tk.Tk(), where tkinterdnd2 can't register a drop
        target — so promising drag-and-drop here would be a lie."""
        app = self.app(root)
        assert "Drag" not in app.empty_hint.cget("text")
        assert "Load CSV" in app.empty_hint.cget("text")

    def test_register_drop_target_reports_failure_rather_than_raising(self, root):
        app = self.app(root)
        assert app._register_drop_target(app.song_tree) is False


class TestEmptyListHintWithRealDragAndDrop:
    """The advertised path. Everything above runs on a plain tk.Tk(), where
    tkinterdnd2 can't register a drop target — so without this the "Drag a CSV
    file…" text and both registrations were asserted by nothing at all.
    """

    @pytest.fixture
    def dnd_root(self):
        dnd2 = pytest.importorskip("tkinterdnd2")
        try:
            r = dnd2.TkinterDnD.Tk()
        except tk.TclError as exc:            # no display, or no tkdnd binaries
            pytest.skip(f"no TkinterDnD root: {exc}")
        r.withdraw()
        apply_dark_theme(r)
        yield r
        r.destroy()

    def app(self, dnd_root):
        from cratefill.app import CratefillApp
        app = CratefillApp(dnd_root, startup=False)
        dnd_root.update()
        return app

    def test_it_advertises_dropping(self, dnd_root):
        app = self.app(dnd_root)
        assert "Drag CSV files or music folders here" in app.empty_hint.cget("text")

    def test_both_the_tree_and_the_hint_accept_drops(self, dnd_root):
        """tkdnd exposes registration as a <<Drop>> binding. The hint needs its
        own: it covers the middle of the region the text tells you to aim at."""
        app = self.app(dnd_root)
        assert "<<Drop>>" in app.song_tree.bind()
        assert "<<Drop>>" in app.empty_hint.bind()

    def test_registration_reports_success(self, dnd_root):
        app = self.app(dnd_root)
        assert app._register_drop_target(app.song_tree) is True

    def test_a_hint_that_cannot_accept_drops_is_not_advertised(self, dnd_root, monkeypatch):
        """The gap this closes: if the label's registration failed while the
        tree's succeeded, the old code still said "drag here" over a dead zone."""
        from cratefill.app import CratefillApp
        real = CratefillApp._register_drop_target
        calls = []

        def fail_on_the_label(self, widget):
            calls.append(widget)
            return False if len(calls) > 1 else real(self, widget)

        monkeypatch.setattr(CratefillApp, "_register_drop_target", fail_on_the_label)
        app = CratefillApp(dnd_root, startup=False)
        dnd_root.update()
        assert "Drag" not in app.empty_hint.cget("text")
        assert "Load CSV" in app.empty_hint.cget("text")


class TestManyCandidates:
    """Every version of a song is listed, which can be ten: the list scrolls and
    the buttons must stay on screen."""

    def many(self):
        return MatchDecision(
            "ambiguous",
            candidate=candidate("v0", "Wonderwall", score=1.0),
            reasons=["10 versions of this song found"],
            alternatives=[candidate(f"v{i}", f"Wonderwall (Take {i})",
                                    extras={"album": {"name": "Morning Glory"},
                                            "duration": "4:18", "year": "1995"})
                          for i in range(1, 10)],
        )

    def test_the_dialog_fits_on_screen(self, root):
        dialog = open_dialog(root, self.many())
        assert dialog.winfo_reqheight() <= dialog.winfo_screenheight()
        dialog.destroy()

    def test_every_candidate_is_still_listed(self, root):
        dialog = open_dialog(root, self.many())
        assert len(dialog.choice_vars) == 10
        assert "Wonderwall (Take 9)" in " | ".join(shown_text(dialog))
        dialog.destroy()

    def test_a_short_list_does_not_scroll(self, root, decision):
        dialog = open_dialog(root, decision)
        assert "TScrollbar" not in {w.winfo_class() for w in all_widgets(dialog)}
        dialog.destroy()


def all_widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from all_widgets(child)


class TestWorkerMessages:
    """_poll_worker hands each queued message to its handler on the main thread."""

    def app(self, root):
        from cratefill.app import CratefillApp
        return CratefillApp(root, startup=False)

    def drain(self, root, app, *messages):
        for message in messages:
            app.worker_queue.put(message)
        app._poll_worker()
        root.update()

    def test_log_account_and_connection_reach_the_window(self, root):
        app = self.app(root)
        client = object()
        self.drain(root, app, ("log", "hello from a job"), ("connected", client),
                   ("account", "Login expired? Re-log in."))
        assert "hello from a job" in app.log_text.get("1.0", "end")
        assert app.yt is client
        assert app.login_button.cget("text") == "Re-log in…"
        assert app.account_label.cget("text") == "Login expired? Re-log in."

    def test_a_silent_connect_failure_just_clears_the_client(self, root):
        app = self.app(root)
        app.yt = object()
        self.drain(root, app, ("connect_failed", (True, "expired")))
        assert app.yt is None

    def test_the_review_waits_for_its_jobs_done(self, root, monkeypatch):
        """Phase two may only start once phase one's "done" has cleared
        self.working — so the decisions are held until then."""
        app = self.app(root)
        reviews = []
        monkeypatch.setattr(app, "_review_and_add", lambda *r: reviews.append(r))
        app._start_work()
        self.drain(root, app, ("decisions", ("yt", [], [])))
        assert reviews == [] and app.working
        self.drain(root, app, ("done", None))
        assert reviews == [("yt", [], [])]
        assert not app.working


class TestLoadingSeveralSources:
    def app(self, root):
        from cratefill.app import CratefillApp
        return CratefillApp(root, startup=False)

    @staticmethod
    def csv(tmp_path, name, rows):
        path = tmp_path / name
        path.write_text("Artist,Title\n" + "".join(f"{a},{t}\n" for a, t in rows))
        return str(path)

    def test_several_files_become_one_list(self, root, tmp_path):
        app = self.app(root)
        a = self.csv(tmp_path, "a.csv", [("Phoenix", "Lisztomania")])
        b = self.csv(tmp_path, "b.csv", [("Air", "Sexy Boy"), ("Phoenix", "Lisztomania")])
        app.load_paths([a, b])
        assert [app.song_tree.item(i, "values")[1] for i in app.song_tree.get_children()] \
            == ["Lisztomania", "Sexy Boy"]
        assert app.csv_label.cget("text") == "a.csv + 1 more — 2 songs"
        assert "1 duplicate(s) skipped" in app.log_text.get("1.0", "end")

    def test_a_drop_loads_every_item(self, root, tmp_path):
        app = self.app(root)
        a = self.csv(tmp_path, "with space.csv", [("Phoenix", "Lisztomania")])
        b = self.csv(tmp_path, "b.csv", [("Air", "Sexy Boy")])
        # A real Tcl list, as tkdnd sends it — hand-quoting broke on Windows,
        # where an unbraced path's backslashes were read as escapes.
        event = type("Drop", (), {"data": root.tk.call("list", a, b)})()
        app._on_drop(event)
        root.update()                              # the drop is handled just after
        assert len(app.songs) == 2

    def test_nothing_loadable_keeps_the_current_list(self, root, tmp_path, monkeypatch):
        app = self.app(root)
        warnings = []
        monkeypatch.setattr("cratefill.app.messagebox.showwarning",
                            lambda title, text: warnings.append(text))
        app.load_paths([self.csv(tmp_path, "a.csv", [("Phoenix", "Lisztomania")])])
        app.load_paths([str(tmp_path / "notes.mp3")])
        assert len(app.songs) == 1, "a failed load must not empty the list"
        assert "left unchanged" in warnings[0]

    def test_a_partial_load_names_what_was_skipped(self, root, tmp_path, monkeypatch):
        app = self.app(root)
        warnings = []
        monkeypatch.setattr("cratefill.app.messagebox.showwarning",
                            lambda title, text: warnings.append(text))
        app.load_paths([self.csv(tmp_path, "a.csv", [("Phoenix", "Lisztomania")]),
                        str(tmp_path / "empty.csv")])
        assert len(app.songs) == 1
        assert "empty.csv" in warnings[0] and "left unchanged" not in warnings[0]


class TestAddOrReplaceDialog:
    def open(self, root):
        from cratefill.dialogs import AddOrReplaceDialog
        dialog = AddOrReplaceDialog(root, count=2, listed=5)
        root.update()
        return dialog

    def test_it_says_what_is_at_stake(self, root):
        dialog = self.open(root)
        shown = " | ".join(shown_text(dialog))
        assert "already has 5 song(s)" in shown and "2 items you dropped" in shown
        dialog.destroy()

    @pytest.mark.parametrize("button, choice", [
        ("Add and combine to the list", "add"), ("Make a new list", "new")])
    def test_each_button_reports_its_choice(self, root, button, choice):
        dialog = self.open(root)
        [b for b in all_widgets(dialog)
         if b.winfo_class() == "TButton" and b.cget("text") == button][0].invoke()
        assert dialog.choice == choice

    def test_return_adds(self, mapped_root):
        dialog = self.open(mapped_root)
        press(mapped_root, dialog, "<Return>")
        assert dialog.choice == "add"

    def test_return_presses_the_focused_button(self, mapped_root):
        """Tab to "Make a new list", Enter: that's the choice, not Add."""
        dialog = self.open(mapped_root)
        for _ in range(3):
            mapped_root.update()
        dialog.new_button.focus_force()
        mapped_root.update()
        dialog.event_generate("<Return>")
        mapped_root.update()
        assert dialog.choice == "new"

    def test_escape_cancels(self, mapped_root):
        dialog = self.open(mapped_root)
        press(mapped_root, dialog, "<Escape>")
        assert not dialog.winfo_exists(), "Escape must close the dialog"
        assert dialog.choice is None


class TestLoadingOntoAList:
    """Loading onto a non-empty list — by drop or by button — asks: combine,
    or make a new list."""

    def app(self, root, tmp_path, monkeypatch, answer):
        from cratefill.app import CratefillApp
        app = CratefillApp(root, startup=False)
        self.asked = []
        monkeypatch.setattr(app, "_ask_add_or_replace",
                            lambda count, how: self.asked.append((count, how)) or answer)
        app.load_paths([TestLoadingSeveralSources.csv(
            tmp_path, "first.csv", [("Phoenix", "Lisztomania"), ("Air", "Sexy Boy")])])
        return app

    @staticmethod
    def drop(app, *paths, settle=True):
        app._on_drop(type("Drop", (), {"data": app.root.tk.call("list", *map(str, paths))})())
        if settle:
            app.root.update()                      # the drop is handled just after

    def test_an_empty_list_does_not_ask(self, root, tmp_path, monkeypatch):
        from cratefill.app import CratefillApp
        app = CratefillApp(root, startup=False)
        monkeypatch.setattr(app, "_ask_add_or_replace",
                            lambda count, how: pytest.fail("nothing to combine with"))
        self.drop(app, TestLoadingSeveralSources.csv(tmp_path, "a.csv", [("Air", "Sexy Boy")]))
        assert len(app.songs) == 1

    def test_add_combines_and_keeps_the_selection(self, root, tmp_path, monkeypatch):
        app = self.app(root, tmp_path, monkeypatch, "add")
        app.song_tree.selection_set("1")                     # Air — Sexy Boy
        second = TestLoadingSeveralSources.csv(
            tmp_path, "second.csv", [("Air", "Sexy Boy"), ("Daft Punk", "One More Time")])
        self.drop(app, second)
        assert self.asked == [(1, "dropped")]
        assert [s.title for s in app.songs] == ["Lisztomania", "Sexy Boy", "One More Time"]
        assert app.song_tree.selection() == ("1",)
        assert app.csv_label.cget("text") == "first.csv + 1 more — 3 songs"
        assert "1 duplicate(s) skipped" in app.log_text.get("1.0", "end")

    def test_new_list_replaces(self, root, tmp_path, monkeypatch):
        app = self.app(root, tmp_path, monkeypatch, "new")
        self.drop(app, TestLoadingSeveralSources.csv(tmp_path, "b.csv", [("Daft Punk", "Da Funk")]))
        assert [s.title for s in app.songs] == ["Da Funk"]
        assert app.csv_label.cget("text") == "b.csv — 1 songs"

    def test_dismissing_the_question_loads_nothing(self, root, tmp_path, monkeypatch):
        app = self.app(root, tmp_path, monkeypatch, None)
        self.drop(app, TestLoadingSeveralSources.csv(tmp_path, "b.csv", [("Daft Punk", "Da Funk")]))
        assert [s.title for s in app.songs] == ["Lisztomania", "Sexy Boy"]

    @pytest.mark.parametrize("button, answer, titles", [
        ("load_csv", "add", ["Lisztomania", "Sexy Boy", "Da Funk"]),
        ("load_csv", "new", ["Da Funk"]),
        ("load_csv", None, ["Lisztomania", "Sexy Boy"]),
        ("load_folder", "add", ["Lisztomania", "Sexy Boy", "Da Funk"]),
    ])
    def test_the_buttons_ask_too(self, root, tmp_path, monkeypatch, button, answer, titles):
        app = self.app(root, tmp_path, monkeypatch, answer)
        if button == "load_csv":
            picked = TestLoadingSeveralSources.csv(tmp_path, "b.csv", [("Daft Punk", "Da Funk")])
            monkeypatch.setattr("cratefill.app.filedialog.askopenfilenames",
                                lambda **kw: (picked,))
        else:
            folder = tmp_path / "Daft Punk"
            folder.mkdir()
            (folder / "Da Funk.mp3").touch()
            monkeypatch.setattr("cratefill.app.filedialog.askdirectory",
                                lambda **kw: str(folder))
        getattr(app, button)()
        assert self.asked == [(1, "selected")]
        assert [s.title for s in app.songs] == titles

    def test_a_cancelled_file_picker_asks_nothing(self, root, tmp_path, monkeypatch):
        app = self.app(root, tmp_path, monkeypatch, "add")
        monkeypatch.setattr("cratefill.app.filedialog.askopenfilenames", lambda **kw: ())
        app.load_csv()
        assert self.asked == [] and len(app.songs) == 2

    def test_the_drop_callback_returns_before_asking(self, root, tmp_path, monkeypatch):
        """On Windows the drag source waits for the callback: no question
        may be asked inside it."""
        app = self.app(root, tmp_path, monkeypatch, "add")
        self.drop(app, TestLoadingSeveralSources.csv(tmp_path, "b.csv", [("Daft Punk", "Da Funk")]),
                  settle=False)
        assert self.asked == [], "asked inside the drop callback"
        root.update()
        assert self.asked == [(1, "dropped")]

    def test_a_second_drop_while_one_is_pending_is_ignored(self, root, tmp_path, monkeypatch):
        app = self.app(root, tmp_path, monkeypatch, "add")
        b = TestLoadingSeveralSources.csv(tmp_path, "b.csv", [("Daft Punk", "Da Funk")])
        c = TestLoadingSeveralSources.csv(tmp_path, "c.csv", [("Justice", "D.A.N.C.E.")])
        self.drop(app, b, settle=False)
        self.drop(app, c, settle=False)
        root.update()
        assert self.asked == [(1, "dropped")], "only one question at a time"
        assert "Da Funk" in [s.title for s in app.songs]
        assert "D.A.N.C.E." not in [s.title for s in app.songs]
        assert not app.drop_pending, "the next drop must be accepted again"

    def test_a_drop_with_nothing_new_says_so(self, root, tmp_path, monkeypatch):
        app = self.app(root, tmp_path, monkeypatch, "add")
        self.drop(app, TestLoadingSeveralSources.csv(tmp_path, "b.csv", [("Air", "Sexy Boy")]))
        assert len(app.songs) == 2
        assert "Nothing new" in app.log_text.get("1.0", "end")
