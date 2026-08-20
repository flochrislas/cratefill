"""What to do about an ambiguous match, and remembering the user's answer.

Kept apart from matching.py on purpose: matching decides whether a candidate is
*credible*, this module decides what a credible-but-uncertain candidate should
lead to. Pure and Tk-free, so the rules are testable on their own — see
tests/test_policy.py.
"""

from .storage import read_json, user_data_dir, write_json_atomic

ASK, SKIP, ADD = "ask", "skip", "add"
POLICIES = (ASK, SKIP, ADD)
DEFAULT_POLICY = ASK

# Dropdown wording ↔ stored value. The stored values are short and stable so the
# settings file stays readable and the labels can be reworded freely.
POLICY_LABELS = {ASK: "Always ask", SKIP: "Always skip", ADD: "Always add"}
LABEL_POLICIES = {label: value for value, label in POLICY_LABELS.items()}

SETTINGS_KEY = "ambiguous_match_policy"

# Not secret, so it lives beside browser.json rather than inside it — no reason
# for a preference to share a file with session cookies.
SETTINGS_FILE = user_data_dir() / "settings.json"


def load_policy():
    """The saved policy, or "ask" if anything at all is wrong with the file.

    Absent, unreadable, malformed, not a JSON object, or an unrecognised value
    all fall back to the safe default: asking never loses a song.
    """
    data = read_json(SETTINGS_FILE)
    if isinstance(data, dict) and data.get(SETTINGS_KEY) in POLICIES:
        return data[SETTINGS_KEY]
    return DEFAULT_POLICY


def save_policy(value):
    """Persist the policy immediately. Returns True if it was written.

    Preserves any other keys already in the file, so future settings added by a
    newer version aren't wiped by an older one.
    """
    if value not in POLICIES:
        raise ValueError(f"unknown policy {value!r}; expected one of {POLICIES}")
    data = read_json(SETTINGS_FILE)
    settings = data if isinstance(data, dict) else {}
    settings[SETTINGS_KEY] = value
    return write_json_atomic(SETTINGS_FILE, settings)


def action_for_match(decision, ambiguous_policy):
    """Turn a MatchDecision plus the saved policy into "add", "skip" or "ask".

    The policy only ever governs the ambiguous middle. A high-confidence match is
    always added and a rejected one is always skipped — in particular, "Always
    add" must never rescue a match that failed a threshold or conflicts on
    recording version.
    """
    if decision.status == "high":
        return ADD
    if decision.status == "rejected":
        return SKIP
    return ambiguous_policy if ambiguous_policy in POLICIES else DEFAULT_POLICY
