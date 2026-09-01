"""P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a real Docker container
can die on its own between polls -- before this, `sandboxes.status`
only left RUNNING once a human happened to click Check Health, so the
UI could claim RUNNING indefinitely after the real container was long
gone. CleanupWorker.reconcile() now re-verifies every RUNNING sandbox
on its own poll interval. Real docker throughout: a real container is
provisioned, then real `docker kill` simulates the unexpected death
(never ProjectFlow's own stop/cleanup path, which would correctly
update status itself) -- skipped where no real docker daemon is
reachable, same convention as tests/test_sandbox_docker.py."""
from __future__ import annotations
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


if not _docker_available():
    pytestmark = pytest.mark.skip(reason="docker daemon not reachable in this environment")


def register(client, repo):
    return client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": repo.name, "default_branch": "main"})


def create_task_workspace(client, tid, rid, agent="codex", role="Backend", profile="backend"):
    r = client.post(
        f"/api/tasks/{tid}/workspaces",
        data={"repository_id": rid, "agent": agent, "role": role, "base_branch": "main", "sandbox_profile": profile},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return client.get("/api/workspaces").json()[-1]


def test_reconcile_detects_a_container_killed_outside_projectflow(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-dies")
    register(client, repo)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-dies"][0]["id"]
    client.post("/api/tasks", data={"title": "Sandbox dies mid-run"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")

    w = create_task_workspace(client, tid, rid)
    sb = client.get("/api/sandboxes").json()[0]
    try:
        assert sb["status"] == "RUNNING" and sb["health_status"] == "HEALTHY", sb

        # A real, unexpected death -- outside ProjectFlow's own
        # stop/cleanup path entirely (an OOM-kill, a host reboot of just
        # the container runtime, `docker kill` from an operator's own
        # shell -- ProjectFlow never sees this event happen).
        ps = subprocess.run(
            ["docker", "ps", "--filter", f"label=com.docker.compose.project={sb['compose_project']}", "--format", "{{.ID}}"],
            capture_output=True, text=True)
        container_ids = [c for c in ps.stdout.splitlines() if c.strip()]
        assert container_ids, "expected at least one real running container to kill"
        subprocess.run(["docker", "kill", *container_ids], capture_output=True, text=True, check=True)

        # Without any manual "Check Health" click -- just the same
        # reconcile() a real restart or the next poll tick would run.
        client.app.state.cleanup_worker.reconcile()

        after = client.get(f"/api/sandboxes").json()[0]
        assert after["status"] == "UNHEALTHY", f"reconcile() must self-heal a dead container's status, got {after}"
    finally:
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_reconcile_leaves_a_genuinely_healthy_sandbox_running(client, git_repo, sandboxable_repo_factory):
    """The new reconcile step must not be a blunt instrument -- a real,
    still-alive, still-healthy sandbox must stay RUNNING."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-healthy")
    register(client, repo)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-healthy"][0]["id"]
    client.post("/api/tasks", data={"title": "Sandbox stays healthy"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")

    w = create_task_workspace(client, tid, rid)
    sb = client.get("/api/sandboxes").json()[0]
    try:
        assert sb["status"] == "RUNNING" and sb["health_status"] == "HEALTHY", sb
        client.app.state.cleanup_worker.reconcile()
        after = client.get(f"/api/sandboxes").json()[0]
        assert after["status"] == "RUNNING"
        assert after["health_status"] == "HEALTHY"
    finally:
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")
