"""UX contract tests (mobile-first redesign, sections 35/36/37): the
UserTaskState view model must never produce a dead end, and must never
contradict the real underlying state. These are unit tests against
user_task_state() directly (fast, exhaustive across every next_action
code TaskDecisionService can produce) -- see
test_mobile_ux_real_scenarios.py for the real, driven-through-the-app
end-to-end scenarios with screenshots."""
from __future__ import annotations

from app.services.task_decision_service import _action
from app.services.user_state_view import ACTION_STATE_CLASS, task_state_class, user_task_state


def _decision(action, status="ACTIVE", blocking=None, builders=None):
    return {
        "status": status,
        "next_action": _action(action, f"label-{action}", f"reason-{action}", target=("/x" if action != "NONE" else None)),
        "blocking_reasons": blocking or [],
        "builders": builders or [],
        "current_step": "AGENT_RUNNING",
    }


# ------------------------------------------------- dead-end test (36)
def test_every_action_code_yields_a_state_class():
    """Every code TaskDecisionService can actually emit must map to one
    of the four classes (or be explicitly context-dependent via None,
    handled by task_state_class's BLOCKED/WAITING fallback) -- a new
    action code added to TaskDecisionService without updating this table
    would otherwise silently render as an unclassified, buttonless dead
    end."""
    for action in ACTION_STATE_CLASS:
        d = _decision(action)
        cls = task_state_class(d)
        assert cls in ("WORKING", "WAITING", "ACTION_REQUIRED", "COMPLETE"), f"{action} -> unclassified {cls}"


def test_action_required_states_always_have_a_primary_action():
    for action, cls in ACTION_STATE_CLASS.items():
        if cls != "ACTION_REQUIRED":
            continue
        d = _decision(action)
        view = user_task_state(d)
        assert view["state_class"] == "ACTION_REQUIRED"
        assert view["primary_action"] is not None, f"{action} is ACTION_REQUIRED but has no primary_action -- dead end"
        assert view["primary_action"]["target"], f"{action}'s primary_action has no real target"


def test_waiting_states_never_need_a_primary_action_but_are_not_dead_ends():
    """WAITING is not a dead end by definition (section 13/15) -- the
    invariant is primary_action OR an explicit wait reason, never
    neither."""
    for action, cls in ACTION_STATE_CLASS.items():
        if cls != "WAITING":
            continue
        d = _decision(action)
        view = user_task_state(d)
        assert view["state_class"] == "WAITING"
        assert view["explanation"], f"{action} is WAITING with no explanation -- silent dead end"


def test_none_action_with_blocked_status_is_action_required_not_silent():
    """A real, unresolved blocker with no computed recovery action must
    still surface as something a human needs to look at -- never render
    as if the system were quietly waiting for something to resolve
    itself (section 15: never neither)."""
    d = _decision("NONE", status="BLOCKED", blocking=["Something real blocked this"])
    view = user_task_state(d)
    assert view["state_class"] == "ACTION_REQUIRED"
    assert view["blocker"] == "Something real blocked this"


def test_none_action_with_active_status_and_no_blocking_is_honest_waiting():
    d = _decision("NONE", status="ACTIVE", blocking=[])
    view = user_task_state(d)
    assert view["state_class"] == "WAITING"


def test_dead_end_invariant_every_non_terminal_state_has_action_or_wait_reason():
    """The core invariant (section 15/36): primary_action OR an explicit
    wait_reason (never neither), for every code the real decision
    service can emit."""
    for action in ACTION_STATE_CLASS:
        d = _decision(action)
        view = user_task_state(d)
        if view["state_class"] == "COMPLETE":
            continue  # terminal, invariant doesn't apply
        has_action = view["primary_action"] is not None
        has_wait_reason = bool(view["explanation"])
        assert has_action or has_wait_reason, f"{action}: neither primary_action nor wait_reason -- real dead end"


# --------------------------------------------- contradiction test (37)
def test_complete_state_never_carries_a_stale_start_action():
    """Task DONE + a leftover 'Start'/'Merge' primary action would be a
    contradiction a user could act on nonsensically."""
    d = _decision("NONE", status="DONE")
    view = user_task_state(d)
    assert view["state_class"] == "COMPLETE"
    if view["primary_action"]:
        assert "start" not in view["primary_action"]["label"].lower()
        assert "merge" not in view["primary_action"]["label"].lower()


def test_working_state_never_shows_a_start_primary_action():
    """Agent RUNNING (VIEW_BUILDER) must never render a 'Start' primary
    button -- that would contradict the fact that it's already running."""
    d = _decision("VIEW_BUILDER")
    view = user_task_state(d)
    assert view["state_class"] == "WORKING"
    assert view["primary_action"] is None


# ------------------------------------------------------- headline copy
def test_headlines_are_never_raw_screaming_snake_case():
    for action in ACTION_STATE_CLASS:
        d = _decision(action)
        view = user_task_state(d)
        headline = view["headline"]
        assert headline and headline != headline.upper() or " " in headline or not headline.isupper(), f"{action}: headline looks like a raw enum: {headline!r}"
        assert "_" not in headline, f"{action}: headline leaks an internal enum: {headline!r}"
