"""Tests for the build self-check.

Its whole job is to fail loudly when a frozen build is missing something, so
these tests mostly prove that it *does* fail — a self-test that always passes is
worse than none.
"""

import io

import pytest

from cratefill import app, selftest


def report(monkeypatch=None):
    buf = io.StringIO()
    passed = selftest.run(out=buf)
    return passed, buf.getvalue()


def with_broken(monkeypatch, name, exc):
    """Replace one check with a failing one, leaving the rest alone."""
    def boom():
        raise exc
    checks = tuple((n, i, boom if n == name else c) for n, i, c in selftest.CHECKS)
    monkeypatch.setattr(selftest, "CHECKS", checks)


class TestPassingBuild:
    def test_this_environment_passes(self):
        passed, text = report()
        assert passed, text
        assert "PASS" in text

    def test_every_check_is_reported(self):
        _passed, text = report()
        for name, _importance, _check in selftest.CHECKS:
            assert name in text

    def test_main_returns_a_zero_exit_code(self):
        assert selftest.main() == 0

    def test_it_says_which_python_and_platform(self):
        _passed, text = report()
        assert "Python" in text and "self-test" in text


class TestFrozenBuildFailures:
    """The two failures this exists to catch, both invisible in a --windowed exe
    because they happen at import time before any window appears."""

    def test_missing_rapidfuzz_fails_the_run(self, monkeypatch):
        with_broken(monkeypatch, "rapidfuzz",
                    ModuleNotFoundError("No module named 'rapidfuzz'"))
        passed, text = report()
        assert passed is False
        assert "FAIL" in text and "rapidfuzz" in text

    def test_missing_ytmusicapi_locales_fails_the_run(self, monkeypatch):
        """--collect-all ytmusicapi is what bundles the gettext catalogues; the
        real error is this misleading FileNotFoundError."""
        with_broken(monkeypatch, "ytmusicapi",
                    FileNotFoundError("No translation file found for domain: 'base'"))
        passed, text = report()
        assert passed is False
        assert "translation file" in text

    @pytest.mark.parametrize("name", [n for n, i, _ in selftest.CHECKS if i == selftest.REQUIRED])
    def test_any_required_check_can_fail_the_run(self, monkeypatch, name):
        with_broken(monkeypatch, name, RuntimeError("broken"))
        passed, _text = report()
        assert passed is False

    def test_main_returns_one_when_something_is_broken(self, monkeypatch):
        with_broken(monkeypatch, "matching", RuntimeError("broken"))
        assert selftest.main() == 1


class TestOptionalPieces:
    def test_missing_drag_and_drop_warns_without_failing(self, monkeypatch):
        """tkinterdnd2 is optional everywhere else, so it must not fail a build."""
        monkeypatch.setattr(app, "TkinterDnD", None)
        passed, text = report()
        assert passed is True
        assert "[warn] tkinterdnd2" in text

    def test_no_display_skips_tkinter_without_failing(self, monkeypatch):
        """A headless machine is not a broken build — this has to stay usable in
        CI, where there may be no display at all.

        Simulated by making Tk() raise, rather than by unsetting DISPLAY: that
        variable is an X11 concept, so on Windows deleting it changes nothing and
        Tk opens a window regardless. Patching the failure itself tests the
        branch that matters on every platform.
        """
        import tkinter as tk

        def no_display(*_a, **_k):
            raise tk.TclError("no display name and no $DISPLAY environment variable")

        monkeypatch.setattr(tk, "Tk", no_display)
        detail = selftest._check_tkinter()
        assert "skipped" in detail
        passed, _text = report()
        assert passed is True


class TestChecksAreMeaningful:
    def test_rapidfuzz_check_needs_a_real_partial_score(self):
        """It must call the scorer, not just import it: a bundled-but-broken
        extension would still import."""
        assert "scoring works" in selftest._check_rapidfuzz()

    def test_matching_check_exercises_both_ends_of_the_pipeline(self):
        detail = selftest._check_matching()
        assert "high" in detail and "rejected" in detail

    def test_storage_check_reports_the_platform_path(self):
        from cratefill.storage import user_data_dir
        assert selftest._check_storage() == str(user_data_dir())
