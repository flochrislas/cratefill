"""Tests for the ambiguous-match policy and its persistence. No Tk, no network."""

import json

import pytest

from cratefill import policy
from cratefill.matching import MatchDecision


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Point the module at a throwaway settings file."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(policy, "SETTINGS_FILE", path)
    return path


class TestLoadPolicy:
    def test_defaults_to_ask_when_there_is_no_file(self, settings):
        assert policy.load_policy() == "ask"

    @pytest.mark.parametrize("contents, label", [
        ("", "empty file"),
        ("{", "truncated json"),
        ("not json at all", "garbage"),
        ("[]", "json but not an object"),
        ('{"ambiguous_match_policy": "maybe"}', "unknown value"),
        ('{"ambiguous_match_policy": null}', "null value"),
        ('{"something_else": "add"}', "key missing"),
    ])
    def test_anything_wrong_falls_back_to_ask(self, settings, contents, label):
        """A bad settings file must never cost the user a song, and must never
        stop the app from starting."""
        settings.write_text(contents, encoding="utf-8")
        assert policy.load_policy() == "ask", label

    @pytest.mark.parametrize("value", ["ask", "skip", "add"])
    def test_reads_each_valid_value(self, settings, value):
        settings.write_text(json.dumps({"ambiguous_match_policy": value}), encoding="utf-8")
        assert policy.load_policy() == value

    def test_unreadable_file_falls_back(self, settings, monkeypatch):
        monkeypatch.setattr(policy, "SETTINGS_FILE", settings.parent)  # a directory
        assert policy.load_policy() == "ask"


class TestSavePolicy:
    @pytest.mark.parametrize("value", ["ask", "skip", "add"])
    def test_round_trip(self, settings, value):
        assert policy.save_policy(value) is True
        assert json.loads(settings.read_text(encoding="utf-8")) == {
            "ambiguous_match_policy": value,
            "settings_version": policy.SETTINGS_VERSION,
        }
        assert policy.load_policy() == value

    def test_creates_the_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(policy, "SETTINGS_FILE", tmp_path / "nested" / "deep" / "s.json")
        assert policy.save_policy("add") is True
        assert policy.load_policy() == "add"

    def test_rejects_an_unknown_value(self, settings):
        with pytest.raises(ValueError):
            policy.save_policy("maybe")
        assert not settings.exists()

    def test_leaves_no_partial_file_behind(self, settings):
        """The write is staged then swapped, so a crash can't truncate it."""
        policy.save_policy("add")
        siblings = [p.name for p in settings.parent.iterdir()]
        assert siblings == ["settings.json"]

    def test_preserves_unrelated_keys(self, settings):
        """A newer version's settings must survive an older version writing."""
        settings.write_text(json.dumps({"future_setting": 42}), encoding="utf-8")
        policy.save_policy("skip")
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data == {"future_setting": 42, "ambiguous_match_policy": "skip",
                        "settings_version": policy.SETTINGS_VERSION}

    def test_reports_failure_instead_of_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(policy, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(policy, "write_json_atomic", lambda *a: False)
        assert policy.save_policy("add") is False


class TestMigrateSettings:
    """"Always add" is a standing instruction to skip review, so it should only
    ever apply to rules the user agreed to."""

    def test_a_stale_add_becomes_ask(self, settings):
        settings.write_text(json.dumps({"ambiguous_match_policy": "add"}), encoding="utf-8")
        assert policy.migrate_settings() is True
        assert policy.load_policy() == "ask"

    def test_it_records_the_version_so_it_only_happens_once(self, settings):
        settings.write_text(json.dumps({"ambiguous_match_policy": "add"}), encoding="utf-8")
        policy.migrate_settings()
        assert json.loads(settings.read_text(encoding="utf-8"))["settings_version"] == \
            policy.SETTINGS_VERSION
        assert policy.migrate_settings() is False

    @pytest.mark.parametrize("value", ["ask", "skip"])
    def test_other_policies_are_left_alone(self, settings, value):
        settings.write_text(json.dumps({"ambiguous_match_policy": value}), encoding="utf-8")
        assert policy.migrate_settings() is False
        assert policy.load_policy() == value

    def test_an_add_saved_by_this_version_survives(self, settings):
        policy.save_policy("add")
        assert policy.migrate_settings() is False
        assert policy.load_policy() == "add"

    def test_no_file_is_nothing_to_migrate(self, settings):
        assert policy.migrate_settings() is False

    def test_a_corrupt_file_is_nothing_to_migrate(self, settings):
        settings.write_text("{ not json", encoding="utf-8")
        assert policy.migrate_settings() is False


class TestActionForMatch:
    def high(self):
        return MatchDecision("high", candidate={"videoId": "v1"})

    def ambiguous(self):
        return MatchDecision("ambiguous", candidate={"videoId": "v1"})

    def weak(self):
        return MatchDecision("weak", candidate={"videoId": "v1"})

    def rejected(self):
        return MatchDecision("rejected")

    @pytest.mark.parametrize("saved", ["ask", "skip", "add"])
    def test_weak_always_asks(self, saved):
        """The "the user can glance at the proposal" rationale only holds if the
        user is actually asked, so the thinnest matches ignore the policy."""
        assert policy.action_for_match(self.weak(), saved) == "ask"

    @pytest.mark.parametrize("saved", ["ask", "skip", "add"])
    def test_high_confidence_ignores_the_policy(self, saved):
        assert policy.action_for_match(self.high(), saved) == "add"

    @pytest.mark.parametrize("saved", ["ask", "skip", "add"])
    def test_rejected_ignores_the_policy(self, saved):
        """"Always add" means "accept credible but uncertain candidates" — never
        "add the first unrelated search result"."""
        assert policy.action_for_match(self.rejected(), saved) == "skip"

    @pytest.mark.parametrize("saved, expected", [
        ("ask", "ask"),
        ("skip", "skip"),
        ("add", "add"),
    ])
    def test_ambiguous_follows_the_policy(self, saved, expected):
        assert policy.action_for_match(self.ambiguous(), saved) == expected

    def test_an_unknown_policy_falls_back_to_asking(self):
        assert policy.action_for_match(self.ambiguous(), "nonsense") == "ask"


class TestLabels:
    def test_every_policy_has_a_label_and_maps_back(self):
        for value in policy.POLICIES:
            label = policy.POLICY_LABELS[value]
            assert policy.LABEL_POLICIES[label] == value

    def test_ask_is_the_default(self):
        assert policy.DEFAULT_POLICY == "ask"
