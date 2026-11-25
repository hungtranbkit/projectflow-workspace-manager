from tests.test_orchestration_v2_dispatch import _make_exited_v2_recovery_task

def test_native_cleanup_reconciles_exhausted_goal_once_and_shows_blocker(client, git_repo):
    _, repo = git_repo
    db, svc, change, task, _ = _make_exited_v2_recovery_task(client, repo)
    svc.provider_available = lambda _: True
    for i in (1, 2):
        db.event("task", task, "GOAL_RESUME_ATTEMPT", f"attempt={i}")
    for _ in range(2):
        client.app.state.cleanup_worker.reconcile()
    count = lambda sql, args: db.one(sql, args)["n"]
    blocks = count("SELECT count(*) n FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='GOAL_RESUME_BLOCKED' AND details LIKE '%RETRY_BUDGET_EXHAUSTED%'", (task,))
    sessions = count("SELECT count(*) n FROM agent_sessions WHERE task_id=?", (task,))
    attempts = count("SELECT count(*) n FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='GOAL_RESUME_ATTEMPT'", (task,))
    ticks = count("SELECT count(*) n FROM workspace_events WHERE action='SUPERVISOR_TICK_FINISHED'", ())
    page = client.get(f"/changes/{change}?mode=simple")
    print(f"NATIVE_CALLBACK ticks={ticks} task={task} blocker_events={blocks} sessions={sessions} attempts={attempts} ui={page.status_code} ui_blocker={'RETRY_BUDGET_EXHAUSTED' in page.text}")
    assert ticks >= 2 and (blocks, sessions, attempts) == (1, 1, 2)
    assert page.status_code == 200 and "RETRY_BUDGET_EXHAUSTED" in page.text

def test_native_cleanup_recovers_and_starts_one_local_canary(client, git_repo, tmp_path):
    import shlex
    import time
    from tests.test_orchestration_v2_dispatch import AgentLauncher
    _, repo = git_repo
    db, svc, change, task, old_session = _make_exited_v2_recovery_task(client, repo)
    svc.provider_available = lambda _: True
    marker = tmp_path / "local-process-starts"
    run = "printf 'started\\n' >> " + shlex.quote(str(marker)) + "; sleep 6"
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", run))}
    client.app.state.cleanup_worker.reconcile()
    limit = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < limit:
        time.sleep(0.05)
    assert marker.exists(), "No receipt from the local canary process"
    client.app.state.cleanup_worker.reconcile()
    rows = db.all("SELECT id,status FROM agent_sessions WHERE task_id=? ORDER BY id", (task,))
    starts = marker.read_text().splitlines()
    actions = db.one("SELECT count(*) n FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='GOAL_RESUME_ATTEMPT'", (task,))["n"]
    print(f"NATIVE_RECOVERY task={task} old={old_session} sessions={len(rows)} new={rows[-1]['id']} actual_process_starts={len(starts)} retry_events={actions}")
    assert starts == ["started"] and len(rows) == 2 and actions == 1 and rows[0]["id"] == old_session
