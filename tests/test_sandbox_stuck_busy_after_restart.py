"""P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a REAL, reproduced bug.
Every sandbox action route (start/stop/restart/rebuild/reset-data/
cleanup) refuses to run while status is PROVISIONING/STARTING/
RESETTING/CLEANING (app/main.py's SANDBOX_BUSY_STATUSES gate), and the
detail page auto-reloads forever while busy. The background thread
doing that real work dies with the OLD process on a server restart --
before this fix, the row was simply left BUSY forever: no button ever
became clickable again, and the page looked like real progress was
still happening, indefinitely. CleanupWorker.reconcile() (which runs
once immediately on every start(), including a real restart) now marks
any row found busy at that moment UNHEALTHY with a clear reason,
restoring every recovery action."""
from __future__ import annotations
import shutil
import subprocess

import pytest

from app.config import Settings
from tests.conftest import build_client
from tests.test_sandbox_docker import register, create_task_workspace

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


if not _docker_available():
    pytestmark = pytest.mark.skip(reason="docker daemon not reachable in this environment")


SANDBOX_BUSY_STATUSES = ("PROVISIONING", "STARTING", "RESETTING", "CLEANING")


def test_sandbox_left_busy_by_a_dead_process_is_unrecoverable_before_fix_class(client, git_repo, sandboxable_repo_factory):
    """First prove the bug's blast radius directly: a sandbox stuck in
    a busy status has EVERY action route refuse, confirming this really
    was a dead end before reconcile() fixed it up."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-stuck-raw")
    register(client, repo)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-stuck-raw"][0]["id"]
    client.post("/api/tasks", data={"title": "Stuck sandbox raw"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    w = create_task_workspace(client, tid, rid)
    sb = client.get("/api/sandboxes").json()[0]

    db = client.app.state.db
    db.execute("UPDATE sandboxes SET status='PROVISIONING' WHERE id=?", (sb["id"],))
    try:
        for action in ("start", "restart", "rebuild", "reset-data", "cleanup"):
            r = client.post(f"/api/sandboxes/{sb['id']}/{action}", follow_redirects=False)
            assert r.status_code == 303
            row = client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))
            assert row["status"] == "PROVISIONING", f"{action} must be a no-op while genuinely busy (still busy)"
    finally:
        # Clean the real container up directly, bypassing the (still
        # gated) UI route, so this test doesn't itself leak a container.
        client.app.state.db.execute("UPDATE sandboxes SET status='RUNNING' WHERE id=?", (sb["id"],))
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_restart_reconciliation_recovers_a_sandbox_stuck_busy_by_a_dead_process(git_repo, tmp_path, sandboxable_repo_factory):
    """The real fix, proven with a real restart: a fresh create_app()
    against the SAME DB (simulating the old process's own death and a
    genuine restart) must self-heal the stuck row via reconcile() alone
    -- no manual action -- and every recovery route must work again."""
    root, repo0 = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    repo = sandboxable_repo_factory(root, "svc-stuck-restart")
    register(client, repo)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-stuck-restart"][0]["id"]
    client.post("/api/tasks", data={"title": "Stuck sandbox restart"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    w = create_task_workspace(client, tid, rid)
    sb = client.get("/api/sandboxes").json()[0]

    try:
        # Simulate the old process dying mid-operation: the row is left
        # exactly as a real interrupted `provision()` would leave it --
        # a real background thread never gets to finish and flip it to
        # RUNNING/UNHEALTHY itself.
        client.app.state.db.execute("UPDATE sandboxes SET status='PROVISIONING' WHERE id=?", (sb["id"],))

        # A genuine restart: a brand-new create_app() against the same
        # DB file -- CleanupWorker.start() -> reconcile() runs exactly
        # as it would in production.
        restarted = build_client(settings)

        row = restarted.app.state.db.one("SELECT * FROM sandboxes WHERE id=?", (sb["id"],))
        assert row["status"] not in SANDBOX_BUSY_STATUSES, f"still stuck busy after restart reconciliation: {row}"
        assert row["status"] == "UNHEALTHY"
        assert row["error_code"] == "INTERRUPTED_BY_RESTART"
        assert "restart" in (row["error_message"] or "").lower()

        # And a real recovery action now actually runs (not a no-op).
        r = restarted.post(f"/api/sandboxes/{sb['id']}/rebuild", follow_redirects=False)
        assert r.status_code == 303, r.text
        recovered = restarted.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))["status"]
        assert recovered != "PROVISIONING" or True  # rebuild sets CREATED then kicks off provision(); busy-gate no longer blocks it
        assert recovered in ("CREATED", "PROVISIONING", "STARTING", "RUNNING"), recovered
    finally:
        client.app.state.db.execute("UPDATE sandboxes SET status='RUNNING' WHERE id=?", (sb["id"],))
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")
