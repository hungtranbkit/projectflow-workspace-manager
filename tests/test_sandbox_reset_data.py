"""Sandbox RESET_DATA (Task #5 demo gaps 6-9): a first-class, audited
operation through SandboxManager -- real docker (nginx:alpine, already
present locally, no network pull), never a raw `docker compose down -v`
exposed to the browser. Covers ownership validation, port persistence,
identity preservation, unrelated-sandbox safety, and audit evidence."""
from __future__ import annotations
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


if not _docker_available():
    pytestmark = pytest.mark.skip(reason="docker daemon not reachable in this environment")


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def wait_status(client, sid, statuses, timeout=30):
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = client.app.state.db.one("SELECT * FROM sandboxes WHERE id=?", (sid,))
        if row and row["status"] in statuses:
            return row
        time.sleep(0.2)
    return row


@pytest.fixture
def cleanup_sandboxes(client):
    created = []
    yield created
    for sid in created:
        client.post(f"/api/sandboxes/{sid}/cleanup")


def make_sandbox(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes, port_range, name):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, name, port_range=port_range)
    register(client, repo, name)
    rid = next(r["id"] for r in client.get("/api/repositories").json() if r["repo_name"] == name)
    client.post("/api/tasks", data={"title": f"{name} task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t for t in client.get("/api/tasks").json() if t["title"] == f"{name} task"][0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    sb = [s for s in client.get("/api/sandboxes").json() if s["repository_id"] == rid][0]
    cleanup_sandboxes.append(sb["id"])
    row = wait_status(client, sb["id"], ("RUNNING", "UNHEALTHY", "FAILED"))
    assert row["status"] == "RUNNING", row
    return row


def test_reset_data_preserves_identity_and_port_and_recreates_healthy(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    sb = make_sandbox(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes, (22300, 22319), "reset-basic")
    sid = sb["id"]; slug_before = sb["sandbox_slug"]; project_before = sb["compose_project"]
    ports_before = client.app.state.db.all("SELECT service,host_port FROM sandbox_ports WHERE sandbox_id=? AND released_at IS NULL", (sid,))
    assert ports_before

    r = client.post(f"/api/sandboxes/{sid}/reset-data", follow_redirects=False)
    assert r.status_code == 303

    row = wait_status(client, sid, ("RUNNING", "FAILED"), timeout=40)
    assert row["status"] == "RUNNING", row
    assert row["sandbox_slug"] == slug_before  # identity preserved
    assert row["compose_project"] == project_before

    ports_after = client.app.state.db.all("SELECT service,host_port FROM sandbox_ports WHERE sandbox_id=? AND released_at IS NULL", (sid,))
    assert {(p["service"], p["host_port"]) for p in ports_after} == {(p["service"], p["host_port"]) for p in ports_before}  # same allocated ports

    ops = client.app.state.db.all("SELECT * FROM sandbox_operations WHERE sandbox_id=? AND operation_type='RESET_DATA' ORDER BY id DESC", (sid,))
    assert ops and ops[0]["status"] == "SUCCESS"  # audited evidence, never deleted


def test_reset_data_refuses_unowned_sandbox(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    """Ownership validation (section 7): a sandbox row whose recorded
    compose_project doesn't actually match a real, labeled docker
    compose project must be refused, never blindly torn down."""
    sb = make_sandbox(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes, (22320, 22339), "reset-unowned")
    client.app.state.db.execute("UPDATE sandboxes SET compose_project=? WHERE id=?", ("not-a-real-project-xyz", sb["id"]))
    with pytest.raises(Exception):
        client.app.state.sandboxes.reset_data(sb["id"])


def test_reset_data_does_not_touch_an_unrelated_sandbox(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    sb1 = make_sandbox(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes, (22340, 22359), "reset-a")
    sb2 = make_sandbox(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes, (22360, 22379), "reset-b")

    r = client.post(f"/api/sandboxes/{sb1['id']}/reset-data", follow_redirects=False)
    assert r.status_code == 303
    wait_status(client, sb1["id"], ("RUNNING", "FAILED"), timeout=40)

    row2 = client.app.state.db.one("SELECT * FROM sandboxes WHERE id=?", (sb2["id"],))
    assert row2["status"] == "RUNNING"  # untouched
    ops2 = client.app.state.db.all("SELECT * FROM sandbox_operations WHERE sandbox_id=? AND operation_type='RESET_DATA'", (sb2["id"],))
    assert not ops2  # never got a RESET_DATA operation of its own
