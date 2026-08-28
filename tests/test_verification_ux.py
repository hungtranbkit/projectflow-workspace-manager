"""Proves the Agent Workspace / Task Detail verification UX: at every
point after an agent finishes coding, the page must say whether a sandbox
exists, what to open, and whether it has actually been verified -- READY
must never read as verified. Real git + real docker (nginx:alpine, no
network pull) throughout, same fixtures as test_task_sandbox_views.py."""
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


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def head(path):
    return run(path, "git", "rev-parse", "HEAD").stdout.strip()


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def wait_agent_tests(client, wid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = client.app.state.db.all("SELECT status FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (wid,))
        if runs and all(x["status"] not in ("QUEUED", "RUNNING") for x in runs): return
        time.sleep(.05)
    raise AssertionError("agent test did not finish")


@pytest.fixture
def cleanup_sandboxes(client):
    created = []
    yield created
    for sid in created:
        client.post(f"/api/sandboxes/{sid}/cleanup")


def make_standalone_ready_workspace(client, root, sandboxable_repo_factory, name, port_range):
    """A legacy (non-Task) Agent Workspace on a repo that DOES declare a
    sandbox: contract -- the exact shape of the real "update-default"
    example: created via /api/workspaces, never through a Task."""
    repo = sandboxable_repo_factory(root, name, port_range=port_range)
    register(client, repo, name)
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/workspaces", data={"repository_id": rid, "agent": "codex", "task_name": name, "base_branch": "main"}, follow_redirects=False)
    assert r.status_code == 303
    w = client.get("/api/workspaces").json()[0]
    client.post(f"/api/workspaces/{w['id']}/ready", follow_redirects=False)
    return w


def test_ready_workspace_without_sandbox_shows_create_sandbox(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    w = make_standalone_ready_workspace(client, root, sandboxable_repo_factory, "verif-a", (21600, 21619))
    page = client.get(f"/workspaces/{w['id']}").text
    assert "NOT CREATED" in page
    assert "Task này chưa có môi trường chạy riêng" in page
    assert 'action="/api/workspaces/%s/create-sandbox"' % w["id"] in page
    assert "Create Sandbox" in page
    assert "chưa có runtime verification" in page  # section 2's explicit next-step text


def test_sandbox_running_displays_urls_and_ports(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    w = make_standalone_ready_workspace(client, root, sandboxable_repo_factory, "verif-b", (21620, 21639))
    r = client.post(f"/api/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    assert sb["status"] == "RUNNING" and sb["health_status"] == "HEALTHY"
    outputs = client.app.state.sandboxes.outputs(sb["id"])
    ports_ = client.app.state.ports.ports_for(sb["id"])

    page = client.get(f"/workspaces/{w['id']}").text
    assert outputs["backend_url"] in page
    assert str(ports_[0]["host_port"]) in page
    assert "RUNNING" in page and "HEALTHY" in page


def test_how_to_verify_renders_and_missing_report_is_honest(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    w = make_standalone_ready_workspace(client, root, sandboxable_repo_factory, "verif-c", (21640, 21659))

    empty_page = client.get(f"/workspaces/{w['id']}").text
    assert "Agent chưa cung cấp hướng dẫn kiểm tra." in empty_page
    assert "Add Verification Instructions" in empty_page
    # never invent a rendered step for a report that doesn't exist (the
    # phrase only appears as a form placeholder, never as a real <li>)
    assert '<li><b>Login using' not in empty_page

    steps = "1. Open the sandbox application.\n2. Login using the configured test/default user.\n3. Verify the new default credential succeeds."
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={
        "work_status": "READY", "what_changed": "Update default password behavior",
        "how_to_verify": steps, "expected_result": "New default credential works", "test_data": "user: admin",
        "runtime_requirements": "BACKEND", "risks": "Old default must stop working",
    }, follow_redirects=False)
    assert r.status_code == 303

    page = client.get(f"/workspaces/{w['id']}").text
    assert "Update default password behavior" in page
    assert "Open the sandbox application." in page
    assert "Login using the configured test/default user." in page
    assert "New default credential works" in page
    assert "Agent chưa cung cấp hướng dẫn kiểm tra." not in page


def test_manual_pass_stores_exact_source_commit_and_stales_on_new_commit(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    w = make_standalone_ready_workspace(client, root, sandboxable_repo_factory, "verif-d", (21660, 21679))
    client.post(f"/api/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    commit_at_verify = head(w["worktree_path"])

    r = client.post(f"/api/sandboxes/{sb['id']}/manual-verification", data={"result": "PASS", "note": "looks good"}, follow_redirects=False)
    assert r.status_code == 303
    row = client.app.state.db.one("SELECT * FROM manual_verifications WHERE sandbox_id=?", (sb["id"],))
    assert row["result"] == "PASS" and row["source_commit"] == commit_at_verify and row["sandbox_id"] == sb["id"]

    page = client.get(f"/workspaces/{w['id']}").text
    assert "PASS" in page

    # a new commit on the same branch must invalidate the PASS -- it never
    # carries across changed code, even though the sandbox is untouched.
    (client.app.state.git.validate_worktree(w["worktree_path"]) / "extra.txt").write_text("more\n")
    run(w["worktree_path"], "git", "add", "."); run(w["worktree_path"], "git", "commit", "-m", "more work")

    stale_page = client.get(f"/workspaces/{w['id']}").text
    assert "STALE" in stale_page
    detail_page = client.get(f"/sandboxes/{sb['id']}").text
    assert "STALE" in detail_page


def test_next_action_progresses_through_every_state(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "verif-e", port_range=(21680, 21699))
    register(client, repo, "verif-e")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/workspaces", data={"repository_id": rid, "agent": "codex", "task_name": "verif-e", "base_branch": "main"})
    w = client.get("/api/workspaces").json()[0]

    # not READY yet
    assert "Agent chưa Mark Ready" in _code(client, w["id"])

    client.post(f"/api/workspaces/{w['id']}/ready")
    assert "Create Sandbox" in client.get(f"/workspaces/{w['id']}").text

    client.post(f"/api/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"})
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    assert "Run Tests" in client.get(f"/workspaces/{w['id']}").text

    client.post(f"/api/workspaces/{w['id']}/test")
    wait_agent_tests(client, w["id"])
    assert "Open App and Verify" in client.get(f"/workspaces/{w['id']}").text

    client.post(f"/api/sandboxes/{sb['id']}/manual-verification", data={"result": "PASS"})
    page = client.get(f"/workspaces/{w['id']}").text
    assert "Create Integration" in page  # verified, standalone workspace -> next is Integration


def _code(client, wid):
    return client.get(f"/workspaces/{wid}").text


def test_ready_never_implies_ready_for_main(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "verif-f", port_range=(21700, 21719))
    register(client, repo, "verif-f")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Ready is not verified"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"})
    w = client.get("/api/workspaces").json()[0]
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    client.post(f"/api/workspaces/{w['id']}/ready")

    page = client.get(f"/tasks/{tid}").text
    assert "READY chỉ có nghĩa" in page
    assert "READY_FOR_MAIN" in page
    assert "<b>NO</b>" in page or ">NO<" in page


def test_workspace_and_task_pages_link_to_ready_next_help(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    w = make_standalone_ready_workspace(client, root, sandboxable_repo_factory, "verif-h", (21740, 21759))
    assert "/help#agent-ready-next" in client.get(f"/workspaces/{w['id']}").text
    assert "READY ≠ task hoàn tất" in client.get("/help").text


def test_task_detail_and_sandbox_detail_show_identical_runtime_identity(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "verif-g", port_range=(21720, 21739))
    register(client, repo, "verif-g")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Identity check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"})
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    outputs = client.app.state.sandboxes.outputs(sb["id"])
    ports_ = client.app.state.ports.ports_for(sb["id"])
    commit = client.app.state.db.one("SELECT commit_sha FROM sandbox_sources WHERE sandbox_id=?", (sb["id"],))["commit_sha"]

    task_page = client.get(f"/tasks/{tid}").text
    sandbox_page = client.get(f"/sandboxes/{sb['id']}").text
    for page in (task_page, sandbox_page):
        assert outputs["backend_url"] in page
        assert str(ports_[0]["host_port"]) in page
        assert commit[:8] in page
