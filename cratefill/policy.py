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
VERSION_KEY = "settings_version"

# Bumped when a release changes what "ambiguous" covers. A saved "add" chosen
# under looser or stricter rules than the current ones shouldn't silently carry
# over — see migrate_settings().
SETTINGS_VERSION = 2

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
    settings[VERSION_KEY] = SETTINGS_VERSION
    return write_json_atomic(SETTINGS_FILE, settings)


def migrate_settings():
    """Reset a stale "add" to "ask". Returns True if it changed anything.

    "Always add" is a standing instruction to skip review, so it should only ever
    apply to rules the user actually agreed to. This release widened what counts
    as ambiguous — wrong artists and different recordings are now offered rather
    than refused — so an "add" saved before that reverts to asking once, and the
    user can opt in again knowing what it now covers.
    """
    data = read_json(SETTINGS_FILE)
    if not isinstance(data, dict):
        return False
    if data.get(VERSION_KEY) == SETTINGS_VERSION:
        return False
    changed = data.get(SETTINGS_KEY) == ADD
    data[SETTINGS_KEY] = ASK if changed else data.get(SETTINGS_KEY, DEFAULT_POLICY)
    data[VERSION_KEY] = SETTINGS_VERSION
    write_json_atomic(SETTINGS_FILE, data)
    return changed


def action_for_match(decision, ambiguous_policy):
    """Turn a MatchDecision plus the saved policy into "add", "skip" or "ask".

    The saved policy only governs the `ambiguous` middle:

    * `high`     → add. Never asks, whatever the policy.
    * `rejected` → skip. Never added, whatever the policy.
    * `weak`     → **ask**, whatever the policy. Too thin to automate: a single
                   shared word or a completely different performer shouldn't be
                   able to put a song in a playlist unseen, and the "the user can
                   glance at the proposal" rationale only holds if they are asked.
    * `ambiguous`→ the saved policy.
    """
    if decision.status == "high":
        return ADD
    if decision.status == "rejected":
        return SKIP
    if decision.status == "weak":
        return ASK
    return ambiguous_policy if ambiguous_policy in POLICIES else DEFAULT_POLICY
