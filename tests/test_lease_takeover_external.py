def test_expired_leader_must_not_dispatch_after_successor_takes_over(client, monkeypatch):
    service = client.app.state.autonomous_execution_service
    actions = []
    which = ["first"]
    def reconcile():
        if which[0] == "first":
            service.db.execute("UPDATE orchestration_leases SET expires_at=0 WHERE lease_key=?", ("projectflow-supervisor",))
            which[0] = "successor"
            service.scheduled_reconcile_tick()
            which[0] = "first-returning"
        return [{"outcome": "NO_RECOVERY_REQUIRED"}]
    monkeypatch.setattr(service, "reconcile_exited_goals", reconcile)
    monkeypatch.setattr(service, "tick", lambda: actions.append(which[0]) or {})
    service.scheduled_reconcile_tick()
    print("DISPATCH_CALLERS", actions)
    assert actions == ["successor"]
