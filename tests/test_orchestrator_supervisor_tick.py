from __future__ import annotations

import threading


def test_supervisor_lease_is_durable_and_single_owner(client):
    service = client.app.state.autonomous_execution_service
    second_db = type(service.db)(service.db.path)

    assert service._acquire_supervisor_lease("leader-a") is True
    assert second_db.acquire_lease("projectflow-supervisor", "leader-b", 180) is False
    assert service._release_supervisor_lease("leader-a") is True
    assert second_db.acquire_lease("projectflow-supervisor", "leader-b", 180) is True


def test_scheduled_reconcile_runs_recovery_then_at_most_one_dag_action(client, monkeypatch):
    service = client.app.state.autonomous_execution_service
    calls = []
    monkeypatch.setattr(service, "reconcile_exited_goals", lambda: [{"outcome": "NO_RECOVERY_REQUIRED"}])
    monkeypatch.setattr(service, "tick", lambda: calls.append("tick") or {"launched": True})

    result = service.scheduled_reconcile_tick()

    assert calls == ["tick"]
    assert result["outcome"] == "TICKED"
    assert result["action"]["launched"] is True


def test_scheduled_reconcile_skips_when_another_process_holds_lease(client, monkeypatch):
    service = client.app.state.autonomous_execution_service
    calls = []
    monkeypatch.setattr(service, "reconcile_exited_goals", lambda: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "tick", lambda: calls.append("tick") or {})
    assert service._acquire_supervisor_lease("other-process") is True
    try:
        result = service.scheduled_reconcile_tick()
    finally:
        service._release_supervisor_lease("other-process")

    assert calls == []
    assert result["outcome"] == "LEASE_HELD"


def test_expired_leader_cannot_dispatch_after_successor_takes_over(client, monkeypatch):
    service = client.app.state.autonomous_execution_service
    actions = []
    which = ["first"]

    def reconcile():
        if which[0] == "first":
            service.db.execute(
                "UPDATE orchestration_leases SET expires_at=0 WHERE lease_key=?",
                ("projectflow-supervisor",),
            )
            which[0] = "successor"
            service.scheduled_reconcile_tick()
            which[0] = "first-returning"
        return [{"outcome": "NO_RECOVERY_REQUIRED"}]

    monkeypatch.setattr(service, "reconcile_exited_goals", reconcile)
    monkeypatch.setattr(service, "tick", lambda: actions.append(which[0]) or {"launched": True})

    service.scheduled_reconcile_tick()

    assert actions == ["successor"]
    assert service.db.one(
        "SELECT action FROM workspace_events WHERE entity_type='system' AND action='SUPERVISOR_TICK_FINISHED' ORDER BY id DESC LIMIT 1"
    )["action"] == "SUPERVISOR_TICK_FINISHED"


def test_expired_leader_is_not_allowed_to_start_builder(client):
    service = client.app.state.autonomous_execution_service
    assert service._acquire_supervisor_lease("stale-owner") is True
    token = service._supervisor_lease_is_current
    service.db.execute("UPDATE orchestration_leases SET expires_at=0 WHERE lease_key=?", ("projectflow-supervisor",))
    assert token("stale-owner") is False


def test_expired_leader_cannot_resume_after_successor_claims_during_recovery(client, git_repo, monkeypatch):
    from tests.test_orchestration_v2_dispatch import _make_exited_v2_recovery_task

    _, repo = git_repo
    db, service, change_id, task_id, old_session = _make_exited_v2_recovery_task(client, repo)
    service.provider_available = lambda _provider: True
    starts = []
    service._start_builder_session = lambda *_args, **_kwargs: starts.append("start") or 999
    successor = "successor-owner"

    def recover_after_takeover():
        db.execute("UPDATE orchestration_leases SET expires_at=0 WHERE lease_key='projectflow-supervisor'")
        assert db.acquire_lease("projectflow-supervisor", successor, 180)
        outcome = service.reconcile_change(change_id)
        assert outcome[0]["outcome"] == "LEASE_LOST"
        return [{"outcome": "NO_RECOVERY_REQUIRED"}]

    monkeypatch.setattr(service, "reconcile_exited_goals", recover_after_takeover)
    result = service.scheduled_reconcile_tick()

    assert result["outcome"] == "LEASE_LOST"
    assert starts == []
    assert db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id=?", (task_id,))["c"] == 1
    assert db.one("SELECT COUNT(*) c FROM workspace_events WHERE entity_type='task' AND entity_id=? "
                  "AND action='GOAL_RESUME_ATTEMPT'", (task_id,))["c"] == 0
    db.release_lease("projectflow-supervisor", successor)
