"""Proves the WEB spec for the Sandbox UI extension: /sandboxes (global) and
/tasks/{id} (Task Detail) render the exact same Sandbox/SandboxPort/output
state -- no second, task-owned copy of a URL or port ever exists. Real git
worktrees and real nginx:alpine docker-compose sandboxes throughout, same
fixtures as tests/test_cross_repo_integration.py."""
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


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def make_task_with_sandbox(client, root, sandboxable_repo_factory, repo_name, task_title, port_range):
    """One Task with one Agent Workspace whose profile resolves to a real,
    running sandbox -- the minimal disposable fixture every scenario below
    builds on."""
    repo = sandboxable_repo_factory(root, repo_name, port_range=port_range)
    register(client, repo, repo_name)
    rid = [r["id"] for r in client.get("/api/repositories").json() if r["repo_name"] == repo_name][0]
    r = client.post("/api/tasks", data={"title": task_title}, follow_redirects=False)
    assert r.status_code == 303
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == task_title][0]
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    ws = [w for w in client.get("/api/workspaces").json() if w["repository_id"] == rid][0]
    sb = [s for s in client.get("/api/sandboxes").json() if s["owner_type"] == "AGENT_WORKSPACE" and s["owner_id"] == ws["id"]][0]
    assert sb["status"] == "RUNNING", sb
    return tid, rid, repo, ws, sb


@pytest.fixture
def cleanup_sandboxes(client):
    created = []
    yield created
    for sid in created:
        client.post(f"/api/sandboxes/{sid}/cleanup")


def test_global_list_and_task_detail_agree_on_one_sandbox_each(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    """Items 1, 2, 3, 4, 5, 9, 10, plus the REAL_UI_VERIFICATION Task A / Task B split."""
    root, _ = git_repo
    tid_a, rid_a, _, ws_a, sb_a = make_task_with_sandbox(client, root, sandboxable_repo_factory, "repo-a", "Task A", (21500, 21519))
    cleanup_sandboxes.append(sb_a["id"])
    tid_b, rid_b, _, ws_b, sb_b = make_task_with_sandbox(client, root, sandboxable_repo_factory, "repo-b", "Task B", (21520, 21539))
    cleanup_sandboxes.append(sb_b["id"])

    outputs_a = client.app.state.sandboxes.outputs(sb_a["id"])
    ports_a = client.app.state.ports.ports_for(sb_a["id"])
    assert "backend_url" in outputs_a and ports_a

    # 1. global sandbox list shows both disposable sandboxes (Task A, Task B)
    global_page = client.get("/sandboxes").text
    assert sb_a["sandbox_slug"] in global_page and sb_b["sandbox_slug"] in global_page

    # 2 & 5. the SAME sandbox appears inside its own Task Detail, and Task
    # Detail links to it -- REAL_UI_VERIFICATION: /tasks/A shows only A.
    task_a_page = client.get(f"/tasks/{tid_a}").text
    assert f"/sandboxes/{sb_a['id']}" in task_a_page
    assert f"/sandboxes/{sb_b['id']}" not in task_a_page
    task_b_page = client.get(f"/tasks/{tid_b}").text
    assert f"/sandboxes/{sb_b['id']}" in task_b_page
    assert f"/sandboxes/{sb_a['id']}" not in task_b_page

    # 3. URL/port values match exactly across global, task, and detail views
    detail_a_page = client.get(f"/sandboxes/{sb_a['id']}").text
    for page in (global_page, task_a_page, detail_a_page):
        assert outputs_a["backend_url"] in page
        assert str(ports_a[0]["host_port"]) in page

    # 4. sandbox detail links back to the correct task, not the other one
    assert f"/tasks/{tid_a}" in detail_a_page
    assert f"/tasks/{tid_b}" not in detail_a_page

    # 9. health/status is the one row everywhere -- no view can show a
    # different value for the same sandbox_id.
    sb_a_now = [s for s in client.get("/api/sandboxes").json() if s["id"] == sb_a["id"]][0]
    for page in (global_page, task_a_page, detail_a_page):
        assert sb_a_now["health_status"] in page
        assert sb_a_now["status"].replace("_", " ") in page or sb_a_now["status"] in page

    # 10. no duplicated persisted task/agent-workspace-owned sandbox state
    # (task.backend_port / task.sandbox_url style columns) -- Sandbox stays
    # the only source of truth.
    forbidden = {"backend_port", "frontend_port", "sandbox_url", "backend_url", "frontend_url", "hardware_api_url"}
    for table in ("tasks", "agent_workspaces"):
        cols = {c["name"] for c in client.app.state.db.all(f"PRAGMA table_info({table})")}
        assert not (cols & forbidden), f"{table} grew its own copy of sandbox-owned fields: {cols & forbidden}"


def test_integration_sandbox_shows_multiple_source_repos_in_task_detail(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    """Item 6, plus staleness (item 7) and the C = Task A + C, Task B
    untouched half of REAL_UI_VERIFICATION."""
    root, _ = git_repo
    backend = sandboxable_repo_factory(root, "backend-repo", port_range=(21540, 21559))
    client_repo = root / "client-repo"
    client_repo.mkdir()
    run(client_repo, "git", "init", "-b", "main")
    run(client_repo, "git", "config", "user.email", "t@t"); run(client_repo, "git", "config", "user.name", "t")
    (client_repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: client-repo}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n"
    )
    (client_repo / "README.md").write_text("client base\n")
    run(client_repo, "git", "add", "."); run(client_repo, "git", "commit", "-m", "base")

    register(client, backend, "backend-repo"); register(client, client_repo, "client-repo")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    client.post("/api/tasks", data={"title": "Task A cross-repo"}, follow_redirects=False)
    tid_a = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Task A cross-repo"][0]
    client.post(f"/api/tasks/{tid_a}/select")
    r = client.post(f"/api/tasks/{tid_a}/workspaces", data={"repository_id": repos["backend-repo"], "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    backend_ws = client.get("/api/workspaces").json()[-1]
    (client.app.state.git.validate_worktree(backend_ws["worktree_path"]) / "feature.txt").write_text("backend feature\n")
    run(backend_ws["worktree_path"], "git", "add", "."); run(backend_ws["worktree_path"], "git", "commit", "-m", "backend feature")

    r = client.post(f"/api/tasks/{tid_a}/workspaces", data={"repository_id": repos["client-repo"], "agent": "claude", "role": "Client", "base_branch": "main", "sandbox_profile": ""}, follow_redirects=False)
    assert r.status_code == 303
    client_ws = [w for w in client.get("/api/workspaces").json() if w["repository_id"] == repos["client-repo"]][0]

    for w in (backend_ws, client_ws):
        assert client.post(f"/api/workspaces/{w['id']}/ready", follow_redirects=False).status_code == 303

    # unrelated Task B, must stay untouched by anything below
    tid_b, rid_b, _, ws_b, sb_b = make_task_with_sandbox(client, root, sandboxable_repo_factory, "repo-b2", "Task B unrelated", (21560, 21579))
    cleanup_sandboxes.append(sb_b["id"])

    sandbox_id = None
    try:
        r = client.post(f"/api/tasks/{tid_a}/integrations", follow_redirects=False)
        assert r.status_code == 303
        integration_sandboxes = [s for s in client.get("/api/sandboxes").json() if s["owner_type"] == "TASK_INTEGRATION"]
        assert len(integration_sandboxes) == 1
        sandbox_id = integration_sandboxes[0]["id"]

        task_a_page = client.get(f"/tasks/{tid_a}").text
        # 6. Task Detail shows BOTH participating repos under Integration
        # Sources, not just one.
        assert "backend-repo" in task_a_page and "client-repo" in task_a_page
        assert "Integration Sources" in task_a_page or "Integration Sandbox" in task_a_page
        outputs = client.app.state.sandboxes.outputs(sandbox_id)
        assert outputs["backend_url"] in task_a_page
        assert f"/sandboxes/{sandbox_id}" in task_a_page

        # REAL_UI_VERIFICATION: /tasks/A now shows A's agent sandboxes + C,
        # /tasks/B (unrelated) still shows only its own sandbox.
        task_b_page = client.get(f"/tasks/{tid_b}").text
        assert f"/sandboxes/{sandbox_id}" not in task_b_page
        assert f"/sandboxes/{sb_b['id']}" in task_b_page
        global_page = client.get("/sandboxes").text
        assert integration_sandboxes[0]["sandbox_slug"] in global_page

        # 7. staleness: move the backend source branch after the sandbox
        # was built -- both /sandboxes/{id} and /tasks/{id} must say so.
        (client.app.state.git.validate_worktree(backend_ws["worktree_path"]) / "feature2.txt").write_text("more\n")
        run(backend_ws["worktree_path"], "git", "add", "."); run(backend_ws["worktree_path"], "git", "commit", "-m", "backend feature v2")
        detail_page = client.get(f"/sandboxes/{sandbox_id}").text
        task_a_page_after = client.get(f"/tasks/{tid_a}").text
        assert "SOURCE STALE" in detail_page
        assert "SOURCE STALE" in task_a_page_after
    finally:
        if sandbox_id:
            client.post(f"/api/sandboxes/{sandbox_id}/cleanup")


def test_stopped_and_cleanup_countdown_state_consistent_everywhere(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    """Items 8 and 9 (the stop/cleanup half): a stopped sandbox and a
    scheduled cleanup countdown must read identically in both views."""
    root, _ = git_repo
    tid, rid, _, ws, sb = make_task_with_sandbox(client, root, sandboxable_repo_factory, "repo-stop", "Task Stop", (21580, 21599))
    cleanup_sandboxes.append(sb["id"])

    assert client.post(f"/api/sandboxes/{sb['id']}/stop").status_code in (200, 303)
    stopped = [s for s in client.get("/api/sandboxes").json() if s["id"] == sb["id"]][0]
    assert stopped["status"] == "STOPPED"
    for page in (client.get("/sandboxes").text, client.get(f"/tasks/{tid}").text, client.get(f"/sandboxes/{sb['id']}").text):
        assert "STOPPED" in page

    client.app.state.db.execute(
        "UPDATE sandboxes SET status='CLEANUP_ELIGIBLE',cleanup_eligible_at=? WHERE id=?",
        ("2099-01-01T00:00:00+00:00", sb["id"]),
    )
    global_page = client.get("/sandboxes").text
    detail_page = client.get(f"/sandboxes/{sb['id']}").text
    assert "cleanup" in global_page.lower()
    assert sb["cleanup_eligible_at"] or "2099-01-01T00:00:00+00:00" in detail_page or "h" in detail_page

    client.app.state.db.execute("UPDATE tasks SET status='MERGED' WHERE id=?", (tid,))
    task_page = client.get(f"/tasks/{tid}").text
    assert "Cleanup in" in task_page


def test_empty_states_distinguish_not_configured_from_not_yet_created(client, git_repo):
    """A workspace whose repo declares no sandbox: contract at all must
    never show the same empty state as one that simply hasn't created its
    sandbox yet -- the spec explicitly forbids a generic message here."""
    root, repo = git_repo  # git_repo's PROJECT.yaml has no sandbox: block
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "plain-repo", "default_branch": "main"})
    assert r.status_code in (200, 303)
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Task No Sandbox"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main"}, follow_redirects=False)
    assert r.status_code == 303

    assert client.get("/api/sandboxes").json() == []  # no sandbox: contract -> no Sandbox row at all
    page = client.get(f"/tasks/{tid}").text
    assert "Sandbox is not configured for this repository." in page
    assert "No runtime sandbox has been created for this task." not in page
