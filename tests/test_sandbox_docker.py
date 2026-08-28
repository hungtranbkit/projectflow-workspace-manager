"""Real Docker sandbox tests (docs section 61). Requires a working `docker`
CLI with daemon access -- skipped otherwise rather than faked."""
from __future__ import annotations
import shutil
import subprocess
import urllib.request

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


def test_agent_sandbox_provisions_real_container_and_is_healthy(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-a")
    register(client, repo)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-a"][0]["id"]
    client.post("/api/tasks", data={"title": "Single sandbox check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]

    w = create_task_workspace(client, tid, rid)
    sb = client.get("/api/sandboxes").json()[0]
    try:
        assert sb["status"] == "RUNNING", sb
        assert sb["health_status"] == "HEALTHY"
        assert sb["compose_project"].startswith("wm-svc-a-")
        outputs = client.app.state.sandboxes.outputs(sb["id"])
        assert "backend_url" in outputs
        with urllib.request.urlopen(outputs["backend_url"], timeout=5) as resp:
            assert resp.status == 200
        ps = subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={sb['compose_project']}", "--format", "{{.Names}}"], capture_output=True, text=True)
        assert sb["compose_project"] in ps.stdout
    finally:
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_two_agent_sandboxes_are_fully_isolated(client, git_repo, sandboxable_repo_factory):
    """docs section 61 Test 1: different ports, different compose project,
    isolated network, both healthy independently."""
    root, _ = git_repo
    repo_a = sandboxable_repo_factory(root, "svc-a", port_range=(21100, 21149))
    repo_b = sandboxable_repo_factory(root, "svc-b", port_range=(21150, 21199))
    register(client, repo_a); register(client, repo_b)
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}
    client.post("/api/tasks", data={"title": "Isolation check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]

    create_task_workspace(client, tid, repos["svc-a"], agent="codex", role="A")
    create_task_workspace(client, tid, repos["svc-b"], agent="claude", role="B")
    sandboxes = client.get("/api/sandboxes").json()
    assert len(sandboxes) == 2
    try:
        assert sandboxes[0]["compose_project"] != sandboxes[1]["compose_project"]
        for sb in sandboxes:
            assert sb["status"] == "RUNNING"
            assert sb["health_status"] == "HEALTHY"
        ports = {p["host_port"] for sb in sandboxes for p in client.app.state.ports.ports_for(sb["id"])}
        assert len(ports) == 2  # distinct host ports, no collision
        nets = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True).stdout
        for sb in sandboxes:
            assert sb["compose_project"] in nets  # each got its own network
    finally:
        for sb in sandboxes:
            client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_sandbox_capacity_limit_blocks_a_new_sandbox(client, git_repo, sandboxable_repo_factory, monkeypatch):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-cap", port_range=(21200, 21249))
    register(client, repo)
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Capacity check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.app.state.sandboxes.max_running = 1
    try:
        create_task_workspace(client, tid, rid, agent="codex", role="A")
        first = client.get("/api/sandboxes").json()[0]
        assert first["status"] == "RUNNING"
        r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "claude", "role": "B", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
        # workspace itself is created (git worktree is never blocked); only
        # the SANDBOX provisioning step is capacity-gated and lands as FAILED.
        assert r.status_code == 303
        sandboxes = client.get("/api/sandboxes").json()
        assert len(sandboxes) == 2
        second = [s for s in sandboxes if s["id"] != first["id"]][0]
        assert second["status"] == "FAILED"
        assert second["error_code"] == "SANDBOX_CAPACITY_FULL"
    finally:
        for sb in client.get("/api/sandboxes").json():
            client.post(f"/api/sandboxes/{sb['id']}/cleanup")
