"""P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a REAL, reproduced bug
with the widest blast radius in this program's own sweep --
OperationService (app/services/operations.py) is the shared duplicate-
click ledger behind FIVE real action routes: Merge Latest, Mark Ready
for Main, Push Integration, Create PR, Merge PR. `begin()` raises
OperationInProgress (every one of those five routes catches it as a
silent no-op redirect) whenever a QUEUED/RUNNING row already exists for
that exact (entity_type, entity_id, operation_type). These operations
run synchronously within their own request, but the `begin()` INSERT
still commits before the real work runs -- a server process
killed/restarted mid-request leaves that row QUEUED/RUNNING forever,
permanently blocking that exact button for that exact entity/operation
pair. OperationService.reconcile_on_startup() (wired at app startup)
now marks any such row FAILED with a clear reason, restoring the
action."""
from __future__ import annotations

from app.config import Settings
from tests.conftest import build_client
from tests.test_integration_push import register, ready_normal_risk_task_with_integration


def test_stuck_operation_blocks_the_exact_button_before_reconcile(client, git_repo):
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Stuck op raw")
    db = client.app.state.db
    op_id = db.execute(
        "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES(?,?,?,'RUNNING',CURRENT_TIMESTAMP)",
        ("MARK_READY_FOR_MAIN", "integration", iid))

    r = client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    assert r.status_code == 303
    still = db.one("SELECT * FROM operations WHERE id=?", (op_id,))
    assert still["status"] == "RUNNING", "must be a no-op while genuinely in progress (OperationInProgress)"
    i = client.get("/api/integrations").json()[0]
    assert i["status"] != "READY_FOR_MAIN", "the real action must never have run while blocked"


def test_restart_reconciliation_recovers_an_operation_stuck_by_a_dead_process(git_repo, tmp_path):
    root, _ = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Stuck op restart")

    db = client.app.state.db
    op_id = db.execute(
        "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES(?,?,?,'RUNNING',CURRENT_TIMESTAMP)",
        ("MARK_READY_FOR_MAIN", "integration", iid))

    # A genuine restart: a brand-new create_app() against the same DB
    # file -- ops.reconcile_on_startup() runs exactly as it would in
    # production.
    restarted = build_client(settings)

    row = restarted.app.state.db.one("SELECT * FROM operations WHERE id=?", (op_id,))
    assert row["status"] == "FAILED", f"still stuck in-progress after restart reconciliation: {row}"
    assert "restart" in (row["error"] or "").lower()

    # And the exact same button now actually RUNS again -- proven by a
    # brand-new operations row appearing, regardless of whether the
    # underlying readiness check itself then passes or fails for its
    # own legitimate reason (a fresh Integration's tests genuinely
    # haven't run yet here, so a 409 GitSafetyError is the CORRECT,
    # honest outcome -- the point being tested is that it is no longer
    # a silent same-state no-op redirect).
    r = restarted.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    assert r.status_code in (303, 409), r.text
    newest_op = restarted.app.state.db.one(
        "SELECT * FROM operations WHERE entity_type='integration' AND entity_id=? AND operation_type='MARK_READY_FOR_MAIN' ORDER BY id DESC LIMIT 1", (iid,))
    assert newest_op["id"] != op_id, "a fresh operation must actually have been started, not blocked by the stuck one"
    assert newest_op["status"] in ("SUCCEEDED", "FAILED"), "the real action must have actually run to completion this time"
