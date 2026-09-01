"""P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a REAL, reproduced bug,
the same shape as the sandbox/deployment ones this program already
fixed. `/api/integrations/{iid}/test` (app/main.py) refuses to start a
new test run while the most recent `test_runs` row for that
integration is QUEUED/RUNNING. The real background thread
(TestRunner._run) doing that work dies with the OLD process on a
restart mid-run -- before this fix, the row was left QUEUED/RUNNING
forever: every future click on [Run Tests] for that Integration was a
permanent no-op. TestRunner.reconcile_on_startup() (wired at app
startup, same pattern as AgentSessionManager/CleanupWorker/
DeploymentService's own) now marks any such row FAIL with a clear
reason, restoring the ability to run tests again."""
from __future__ import annotations

from app.config import Settings
from tests.conftest import build_client
from tests.test_integration_push import register, ready_normal_risk_task_with_integration


def test_stuck_test_run_blocks_every_new_click_before_reconcile(client, git_repo):
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Stuck test run raw")
    db = client.app.state.db
    run_id = db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit) VALUES(?,?,?,?,?,?)",
        ("integration", iid, "true", "test", "RUNNING", "deadbeef"))

    r = client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    assert r.status_code == 303
    still = db.one("SELECT * FROM test_runs WHERE id=?", (run_id,))
    assert still["status"] == "RUNNING", "must be a no-op while genuinely in progress"
    newest = db.one("SELECT id FROM test_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (iid,))
    assert newest["id"] == run_id, "no new test run should have been queued"


def test_restart_reconciliation_recovers_a_test_run_stuck_by_a_dead_process(git_repo, tmp_path):
    root, _ = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Stuck test run restart")

    db = client.app.state.db
    run_id = db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit) VALUES(?,?,?,?,?,?)",
        ("integration", iid, "true", "test", "RUNNING", "deadbeef"))

    # A genuine restart: a brand-new create_app() against the same DB
    # file -- runner.reconcile_on_startup() runs exactly as it would in
    # production.
    restarted = build_client(settings)

    row = restarted.app.state.db.one("SELECT * FROM test_runs WHERE id=?", (run_id,))
    assert row["status"] == "FAIL", f"still stuck in-progress after restart reconciliation: {row}"
    assert "restart" in (row["stderr_tail"] or "").lower()

    # And a fresh test run now actually queues again (not a no-op).
    r = restarted.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    assert r.status_code == 303, r.text
    newest = restarted.app.state.db.one("SELECT id FROM test_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (iid,))
    assert newest["id"] != run_id, "a fresh test run must actually be queued, not blocked by the stuck one"
