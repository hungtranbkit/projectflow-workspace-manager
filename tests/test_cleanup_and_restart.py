"""Auto-cleanup (docs section 63) and restart-recovery (docs section 64)
real-Docker tests."""
from __future__ import annotations
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


if not _docker_available():
    pytestmark = pytest.mark.skip(reason="docker daemon not reachable in this environment")


def register(client, repo):
    client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": repo.name, "default_branch": "main"})


def _provision_one(client, root, repo):
    register(client, repo)
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Cleanup check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "A", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    return client.get("/api/sandboxes").json()[0]


def test_merged_task_schedules_cleanup_and_worker_removes_only_owned_resources(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-clean", port_range=(21400, 21449))
    sb = _provision_one(client, root, repo)
    assert sb["status"] == "RUNNING"
    compose_project = sb["compose_project"]
    ps = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={compose_project}"], capture_output=True, text=True)
    assert ps.stdout.strip(), "container should be running before cleanup"

    tid = sb["task_id"]
    r = client.post(f"/api/tasks/{tid}/mark-merged", follow_redirects=False)
    assert r.status_code == 303
    sb2 = client.get(f"/api/sandboxes/{sb['id']}").json()
    assert sb2["status"] == "CLEANUP_ELIGIBLE"
    assert sb2["cleanup_eligible_at"]

    # simulate retention already having expired, then run the worker's
    # reconcile pass directly (same code path the periodic thread calls)
    client.app.state.db.execute("UPDATE sandboxes SET cleanup_eligible_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", sb["id"]))
    client.app.state.cleanup_worker.reconcile()

    final = client.get(f"/api/sandboxes/{sb['id']}").json()
    assert final["status"] == "CLOSED"
    assert final["cleaned_at"]

    ps_after = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={compose_project}"], capture_output=True, text=True)
    assert not ps_after.stdout.strip(), "container must actually be gone after cleanup"
    nets = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True).stdout
    assert compose_project not in nets

    # ports released
    ports_after = client.app.state.db.all("SELECT * FROM sandbox_ports WHERE sandbox_id=? AND released_at IS NULL", (sb["id"],))
    assert ports_after == []

    # evidence preserved: operations, events, source manifest all still there
    ops = client.app.state.db.all("SELECT * FROM sandbox_operations WHERE sandbox_id=?", (sb["id"],))
    assert any(o["operation_type"] == "PROVISION" for o in ops)
    assert any(o["operation_type"] == "CLEANUP" for o in ops)
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='sandbox' AND entity_id=?", (sb["id"],))
    assert any(e["action"] == "SANDBOX_CLEANED" for e in events)
    task = client.get(f"/api/tasks/{tid}").json()
    assert task["status"] == "ACTIVE"  # task metadata itself is never touched by sandbox cleanup
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert all(m["merge_status"] == "MERGED" for m in d["merge_records"] if m["required"])


def test_extend_retention_keeps_sandbox_from_being_cleaned(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-extend", port_range=(21450, 21499))
    sb = _provision_one(client, root, repo)
    try:
        client.post(f"/api/tasks/{sb['task_id']}/mark-merged", follow_redirects=False)
        client.app.state.db.execute("UPDATE sandboxes SET cleanup_eligible_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", sb["id"]))
        client.post(f"/api/sandboxes/{sb['id']}/extend-retention", data={"hours": 24}, follow_redirects=False)
        client.app.state.cleanup_worker.reconcile()
        still = client.get(f"/api/sandboxes/{sb['id']}").json()
        assert still["status"] != "CLOSED"
    finally:
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_server_restart_preserves_source_manifest_and_cleanup_eligibility(git_repo, tmp_path, sandboxable_repo_factory):
    """docs section 64: a fresh process (new app instance, same db_path and
    state_dir) must reconcile without losing source manifests or cleanup
    eligibility, and must not re-provision a duplicate sandbox."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-restart", port_range=(21500, 21549))
    db_path = tmp_path / "restart.db"
    state_dir = tmp_path / "state"
    settings = Settings(root, "127.0.0.1", 8765, db_path, 30, configured_state_dir=state_dir)

    client_a = TestClient(create_app(settings))
    sb = _provision_one(client_a, root, repo)
    assert sb["status"] == "RUNNING"
    manifest_before = client_a.app.state.sandboxes.outputs(sb["id"])
    client_a.post(f"/api/tasks/{sb['task_id']}/mark-merged", follow_redirects=False)
    client_a.app.state.db.execute("UPDATE sandboxes SET cleanup_eligible_at=? WHERE id=?", ("2099-01-01T00:00:00+00:00", sb["id"]))

    # --- simulate a full process restart: brand new Settings/Database/app,
    # same underlying SQLite file and state dir.
    client_b = TestClient(create_app(settings))
    try:
        sb_after_restart = client_b.get(f"/api/sandboxes/{sb['id']}").json()
        assert sb_after_restart["status"] == "CLEANUP_ELIGIBLE"  # eligibility survived
        assert sb_after_restart["cleanup_eligible_at"] == "2099-01-01T00:00:00+00:00"  # not yet due -> reconcile() must NOT clean it
        assert client_b.app.state.sandboxes.outputs(sb["id"]) == manifest_before  # manifest intact

        # reconcile() ran once on client_b's own startup already (via
        # cleanup_worker.start()); it must not have touched a not-yet-due sandbox.
        ps = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={sb['compose_project']}"], capture_output=True, text=True)
        assert ps.stdout.strip(), "container from before the restart must still be running, untouched"

        # no duplicate sandbox got created for the same AgentWorkspace across restart
        all_sandboxes = client_b.get("/api/sandboxes").json()
        assert len([s for s in all_sandboxes if s["owner_id"] == sb["owner_id"] and s["owner_type"] == "AGENT_WORKSPACE"]) == 1
    finally:
        client_b.post(f"/api/sandboxes/{sb['id']}/cleanup")
