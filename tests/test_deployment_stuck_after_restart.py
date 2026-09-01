"""P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a REAL, reproduced bug,
the same shape as the sandbox one this program already fixed. Every
deployment recovery route (create_deployment/redeploy/rollback,
app/main.py) refuses to act whenever the latest deployment for that
task/repo/environment is still PENDING/PREPARING/BUILDING/DEPLOYING/
VERIFYING. The real background `spawn()` thread doing that work dies
with the OLD process on a server restart mid-deploy -- before this fix,
the row was left stuck in one of those statuses forever: every one of
those three routes just redirected back to the same stuck deployment,
permanently, for that exact task/repo/environment. DeploymentService.
reconcile_on_startup() (wired at app startup, same place/pattern as
AgentSessionManager's own) now marks any row found in that state
FAILED with a clear reason, restoring every recovery route."""
from __future__ import annotations

from app.config import Settings
from tests.conftest import build_client
from tests.test_deployment import done_task, latest_deployment_of, FakeResp

IN_PROGRESS = ("PENDING", "PREPARING", "BUILDING", "DEPLOYING", "VERIFYING")


def test_deployment_stuck_in_progress_blocks_every_recovery_route_before_reconcile(client, git_repo):
    """First prove the blast radius directly: a deployment stuck
    in-progress makes create/redeploy/rollback all no-ops."""
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-stuck-raw")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    dep = latest_deployment_of(client, tid, rid)
    db = client.app.state.db
    db.execute("UPDATE deployments SET status='DEPLOYING' WHERE id=?", (dep["id"],))

    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303
    still = latest_deployment_of(client, tid, rid)
    assert still["id"] == dep["id"] and still["status"] == "DEPLOYING", "must be a no-op while genuinely in progress"

    r = client.post(f"/api/deployments/{dep['id']}/redeploy", follow_redirects=False)
    assert r.status_code == 303
    assert latest_deployment_of(client, tid, rid)["id"] == dep["id"], "redeploy must be a no-op while genuinely in progress"


def test_restart_reconciliation_recovers_a_deployment_stuck_by_a_dead_process(git_repo, tmp_path):
    """The real fix, proven with a real restart: a fresh create_app()
    against the SAME DB (simulating the old process's own death) must
    self-heal the stuck row via reconcile_on_startup() alone, and a
    fresh deployment attempt must actually run again afterward."""
    root, _ = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    tid, rid, mr, repo = done_task(client, root, "svc-stuck-restart")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)

    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    dep = latest_deployment_of(client, tid, rid)
    db = client.app.state.db

    # Simulate the old process dying mid-deploy: the row is left
    # exactly as a real interrupted deploy() would leave it -- the real
    # background thread never gets to finish and flip it to VERIFIED/
    # FAILED itself.
    db.execute("UPDATE deployments SET status='DEPLOYING' WHERE id=?", (dep["id"],))

    # A genuine restart: a brand-new create_app() against the same DB
    # file -- deployer.reconcile_on_startup() runs exactly as it would
    # in production.
    restarted = build_client(settings)

    row = restarted.app.state.db.one("SELECT * FROM deployments WHERE id=?", (dep["id"],))
    assert row["status"] not in IN_PROGRESS, f"still stuck in-progress after restart reconciliation: {row}"
    assert row["status"] == "FAILED"
    assert "restart" in (row["error"] or "").lower()

    # And a fresh deployment attempt now actually runs again (not a
    # no-op redirect back to the same dead row).
    restarted.app.state.deployer.spawn = lambda fn, args=(): fn(*args)
    restarted.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    r = restarted.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    new_dep = latest_deployment_of(restarted, tid, rid)
    assert new_dep["id"] != dep["id"], "a fresh deployment must actually be created, not blocked by the stuck one"
    assert new_dep["status"] != "DEPLOYING" or new_dep["status"] == "VERIFIED", new_dep
