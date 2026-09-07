"""Content lifecycle state machine tests."""


from app.models.base import CONTENT_TRANSITIONS, ContentStatus, can_transition


def test_happy_path_transitions():
    path = [
        "IDEA", "RESEARCHING", "STRATEGY", "SCRIPTING", "SCRIPT_READY",
        "PRODUCTION", "QC", "APPROVED", "PUBLISHED", "ANALYZING", "LEARNED",
    ]
    for cur, nxt in zip(path, path[1:]):
        assert can_transition(cur, nxt), f"{cur} -> {nxt} should be legal"


def test_illegal_shortcuts_blocked():
    assert not can_transition("IDEA", "PUBLISHED")
    assert not can_transition("IDEA", "LEARNED")
    assert not can_transition("SCRIPTING", "APPROVED")
    assert not can_transition("PUBLISHED", "IDEA")


def test_qc_rejection_allows_regeneration():
    assert can_transition("QC", "SCRIPT_READY")


def test_terminal_states_have_no_exits():
    for terminal in ("LEARNED", "SKIPPED"):
        targets = CONTENT_TRANSITIONS[terminal]
        assert targets == set(), f"{terminal} should be terminal"


def test_unknown_state_is_rejected():
    assert not can_transition("NONEXISTENT", "IDEA")


def test_same_state_idempotent():
    assert can_transition("QC", "QC")


def test_every_status_has_entry_in_table():
    for status in ContentStatus:
        assert status.value in CONTENT_TRANSITIONS
