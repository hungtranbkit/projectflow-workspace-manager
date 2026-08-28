"""Task-first control plane: backlog -> prepare -> development -> review
-> QA -> integration -> ready_for_main -> merged/closed, plus the PTY-
backed web terminal (AgentSession). Real git + real docker (nginx:alpine,
no network pull) for the sandbox-touching tests; the PTY tests use a
monkeypatched launcher registry so they never invoke a real `codex`/
`claude` CLI."""
from __future__ import annotations
import shutil
import subprocess
import time

import pytest

from app.launchers import AgentLauncher

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


@pytest.fixture
def cleanup_sandboxes(client):
    created = []
    yield created
    for sid in created:
        client.post(f"/api/sandboxes/{sid}/cleanup")


def wait_agent_tests(client, wid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = client.app.state.db.all("SELECT status FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (wid,))
        if runs and all(x["status"] not in ("QUEUED", "RUNNING") for x in runs): return
        time.sleep(.05)
    raise AssertionError("agent test did not finish")


# --------------------------------------------------------------- BACKLOG

def test_backlog_task_has_no_worktree_until_selected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "New feature idea", "priority": "HIGH", "tags": "kiosk,urgent", "notes": "just an idea"}, follow_redirects=False)
    assert r.status_code == 303
    t = client.get("/api/tasks").json()[0]
    assert t["status"] == "BACKLOG"
    assert t["priority"] == "HIGH" and "kiosk" in t["tags"]
    assert client.get(f"/api/tasks/{t['id']}").json()["workspaces"] == []

    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post(f"/api/tasks/{t['id']}/workspaces", data={"repository_id": rid, "agent": "codex", "base_branch": "main"}, follow_redirects=False)
    assert r.status_code == 409  # cannot allocate a worktree before Select

    page = client.get(f"/tasks/{t['id']}").text
    assert "Select for Development" in page


def test_selecting_task_moves_to_prepare_and_allows_workspace_creation(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    client.post("/api/tasks", data={"title": "Select me"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]

    r = client.post(f"/api/tasks/{tid}/select", follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "PREPARE"

    r = client.post(f"/api/tasks/{tid}/select", follow_redirects=False)
    assert r.status_code == 409  # already selected, not BACKLOG anymore

    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "base_branch": "main"}, follow_redirects=False)
    assert r.status_code == 303
    assert len(client.get(f"/api/tasks/{tid}").json()["workspaces"]) == 1


def test_prompt_preparation_from_structured_brief(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    client.post("/api/tasks", data={"title": "Update default password"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/brief", data={
        "goal": "Verify the new default password behavior.",
        "acceptance_criteria": "New default works; old default rejected.",
        "risk_profile": "HIGH",
    })
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["brief_goal"] == "Verify the new default password behavior."
    assert t["risk_profile"] == "HIGH"
    assert t["agent_prompt"] == ""  # not auto-generated

    r = client.post(f"/api/tasks/{tid}/generate-prompt", follow_redirects=False)
    assert r.status_code == 303
    t2 = client.get(f"/api/tasks/{tid}").json()
    assert "GOAL" in t2["agent_prompt"] and "Verify the new default password behavior." in t2["agent_prompt"]

    # user can edit the generated prompt before it's ever used to launch an agent
    client.post(f"/api/tasks/{tid}/agent-prompt", data={"agent_prompt": "edited by human"})
    assert client.get(f"/api/tasks/{tid}").json()["agent_prompt"] == "edited by human"


# ------------------------------------------------------------- REVIEW/QA

def _prepared_task_with_workspace(client, root, sandboxable_repo_factory, name, port_range, risk_profile="NORMAL"):
    repo = sandboxable_repo_factory(root, name, port_range=port_range)
    register(client, repo, name)
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": f"Task {name}", "risk_profile": risk_profile}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == f"Task {name}"][0]
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    return tid, w


def test_builder_report_and_start_review_requires_ready_for_review(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    tid, w = _prepared_task_with_workspace(client, root, sandboxable_repo_factory, "review-a", (22100, 22119))
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])

    r = client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"}, follow_redirects=False)
    assert r.status_code == 409  # builder hasn't reported/READY yet

    client.post(f"/api/workspaces/{w['id']}/verification-report", data={
        "work_status": "READY", "what_changed": "did the thing", "files_changed": "app/main.py",
        "tests_run": "pytest -k thing", "how_to_verify": "1. open app", "expected_result": "works", "risks": "none",
    })
    page = client.get(f"/tasks/{tid}").text
    assert "READY FOR REVIEW" in page

    r = client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"}, follow_redirects=False)
    assert r.status_code == 303
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["reviewer_agent"] == "claude"
    assert "did the thing" in w2["review_notes"]  # review prompt includes the builder report
    assert "app/main.py" in w2["review_notes"]


def test_review_pass_then_new_commit_invalidates_it(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    tid, w = _prepared_task_with_workspace(client, root, sandboxable_repo_factory, "review-b", (22120, 22139))
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "REVIEW_PASS", "notes": "looks good"}, follow_redirects=False)
    assert r.status_code == 303
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["review_status"] == "REVIEW_PASS"
    commit_at_review = w2["review_commit"]
    assert commit_at_review == run(w["worktree_path"], "git", "rev-parse", "HEAD").stdout.strip()

    page = client.get(f"/tasks/{tid}").text
    assert "REVIEW_PASS" in page or "PASS" in page

    (client.app.state.git.validate_worktree(w["worktree_path"]) / "more.txt").write_text("x\n")
    run(w["worktree_path"], "git", "add", "."); run(w["worktree_path"], "git", "commit", "-m", "more work")

    stale_page = client.get(f"/tasks/{tid}").text
    assert "SOURCE STALE" in stale_page  # the existing staleness rule already covers a moved commit


def test_fix_required_returns_task_to_development(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    tid, w = _prepared_task_with_workspace(client, root, sandboxable_repo_factory, "review-c", (22140, 22159))
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "FIX_REQUIRED", "notes": "missing null check"}, follow_redirects=False)
    assert r.status_code == 303

    kanban = client.get("/kanban").text
    dev_col = kanban.find(">DEVELOPMENT")
    review_col = kanban.find(">REVIEW")
    title_pos = kanban.find(f"Task review-c")
    assert dev_col < title_pos < review_col  # sent back to Development, not stuck in Review

    page = client.get(f"/tasks/{tid}").text
    assert "missing null check" in page  # findings persisted


def test_qa_gated_behind_review_pass_and_required_only_for_high_risk(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    tid, w = _prepared_task_with_workspace(client, root, sandboxable_repo_factory, "qa-a", (22160, 22179), risk_profile="HIGH")
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])

    r = client.post(f"/api/workspaces/{w['id']}/start-tester", data={"tester_agent": "codex"}, follow_redirects=False)
    assert r.status_code == 409  # no REVIEW_PASS yet

    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "REVIEW_PASS"})

    kanban = client.get("/kanban").text
    assert kanban.find(">QA") < kanban.find("Task qa-a") < kanban.find(">INTEGRATION")  # HIGH risk requires QA

    r = client.post(f"/api/workspaces/{w['id']}/start-tester", data={"tester_agent": "codex"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/api/workspaces/{w['id']}/submit-qa", data={"result": "QA_PASS", "notes": "verified manually"}, follow_redirects=False)
    assert r.status_code == 303
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["qa_status"] == "QA_PASS"


def test_low_risk_task_skips_qa_column(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    tid, w = _prepared_task_with_workspace(client, root, sandboxable_repo_factory, "qa-low", (22180, 22199), risk_profile="LOW")
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "REVIEW_PASS"})
    kanban = client.get("/kanban").text
    assert kanban.find(">INTEGRATION") < kanban.find("Task qa-low")  # LOW risk goes straight past QA


# ------------------------------------------------------------- PTY / WS

def make_workspace(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/workspaces", data={"repository_id": rid, "agent": "codex", "task_name": "pty-check", "base_branch": "main"})
    return client.get("/api/workspaces").json()[0]


def test_pty_session_lifecycle_and_ownership(client, git_repo):
    w = make_workspace(client, git_repo)
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "echo pty-alive; read x; echo got:$x"))}

    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303
    sid = int(r.headers["location"].split("/")[-1])
    row = client.app.state.db.one("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    assert row["workspace_id"] == w["id"]  # tied to the exact worktree's workspace
    assert row["cwd"] == w["worktree_path"]
    time.sleep(0.3)
    assert client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"] in ("RUNNING", "EXITED")

    page = client.get(f"/agents/live").text
    assert "pty-check" in page or str(w["id"]) in page

    r = client.post(f"/api/sessions/{sid}/stop", follow_redirects=False)
    assert r.status_code == 303
    time.sleep(0.2)
    assert client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"] == "EXITED"


def test_multiple_agent_sessions_are_independent(client, git_repo):
    w = make_workspace(client, git_repo)
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "sleep 5"))}
    r1 = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    r2 = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    sid1 = int(r1.headers["location"].split("/")[-1]); sid2 = int(r2.headers["location"].split("/")[-1])
    assert sid1 != sid2
    live1 = client.app.state.agent_sessions.get(sid1); live2 = client.app.state.agent_sessions.get(sid2)
    assert live1 is not None and live2 is not None and live1.pid != live2.pid
    client.post(f"/api/sessions/{sid1}/stop"); client.post(f"/api/sessions/{sid2}/stop")


def test_no_arbitrary_agent_or_command_from_the_browser(client, git_repo):
    """Command safety (section 14): the browser can only ever supply
    workspace_id + mode -- there is no field for cwd, executable, or
    flags, and an agent name outside the trusted registry is rejected."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/workspaces", data={"repository_id": rid, "agent": "gemini", "task_name": "untrusted", "base_branch": "main"})
    w = [x for x in client.get("/api/workspaces").json() if x["agent"] == "gemini"][0]
    # "gemini" is an allowed settings.agents value but has no launcher registered
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 409

    # the create-session endpoint has no argv/cwd/flags form field at all --
    # an extra field like this is simply ignored by FastAPI's Form binding.
    r2 = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE", "argv": "rm -rf /", "cwd": "/etc"}, follow_redirects=False)
    assert r2.status_code == 409  # still rejected -- "gemini" has no launcher regardless of extra fields


def test_session_reconcile_marks_stale_sessions_honestly(client, git_repo):
    w = make_workspace(client, git_repo)
    sid = client.app.state.db.execute(
        "INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status,mode,last_activity_at) VALUES(?,?,?,?,?,?,?,?)",
        (None, w["id"], "codex", "Codex", w["worktree_path"], "RUNNING", "INTERACTIVE", "2020-01-01T00:00:00+00:00"),
    )
    client.app.state.agent_sessions.reconcile_on_startup()
    row = client.app.state.db.one("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    assert row["status"] == "FAILED"  # never falsely reported RUNNING after a restart


def test_mobile_viewport_present_on_key_routes(client, git_repo):
    """Section 18: web terminal / live status must be responsive. The
    shared base.html viewport meta covers every page; spot-check the new
    control-plane routes render it too."""
    root, repo = git_repo
    register(client, repo, "demo")
    client.post("/api/tasks", data={"title": "Mobile smoke"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    for path in ["/tasks", "/kanban", f"/tasks/{tid}", "/agents/live"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'name="viewport"' in r.text, path


def test_full_lifecycle_backlog_to_closed(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    """End-to-end walk of the entire pipeline on one real Task: BACKLOG ->
    PREPARE -> DEVELOPMENT -> REVIEW -> INTEGRATION -> READY_FOR_MAIN ->
    MERGED -> CLOSED, checking the Kanban column at each stage."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "lifecycle", port_range=(22200, 22219))
    register(client, repo, "lifecycle")
    rid = client.get("/api/repositories").json()[0]["id"]

    client.post("/api/tasks", data={"title": "Lifecycle task", "risk_profile": "NORMAL"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "BACKLOG"

    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/brief", data={"goal": "ship it", "acceptance_criteria": "works"})
    client.post(f"/api/tasks/{tid}/generate-prompt")
    assert client.get(f"/api/tasks/{tid}").json()["agent_prompt"]

    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "shipped it"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "REVIEW_PASS"})

    r = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r.status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    client.post(f"/api/integrations/{iid}/test")
    for _ in range(100):
        if client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"] != "TESTING": break
        time.sleep(.05)
    client.post(f"/api/integrations/{iid}/ready-for-main")

    page = client.get(f"/tasks/{tid}").text
    assert "READY_FOR_MAIN:</b> YES" in page

    client.post(f"/api/tasks/{tid}/mark-merged")
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "MERGED"

    r = client.post(f"/api/tasks/{tid}/close", follow_redirects=False)
    assert r.status_code == 303
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["status"] == "CLOSED"

    kanban = client.get("/kanban").text
    assert kanban.find(">DONE") < kanban.find("Lifecycle task")


def test_websocket_view_only_blocks_stdin_and_interactive_allows_it(client, git_repo):
    w = make_workspace(client, git_repo)
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "read x; echo got:$x"))}

    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "VIEW_ONLY"}, follow_redirects=False)
    sid = int(r.headers["location"].split("/")[-1])
    time.sleep(0.2)
    with client.websocket_connect(f"/ws/sessions/{sid}") as ws:
        ws.send_bytes(b"hello\n")
        time.sleep(0.3)
        row = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))
        assert row["status"] == "RUNNING"  # the blocked-forever `read x` never got input, still running
    client.post(f"/api/sessions/{sid}/stop")

    r2 = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    sid2 = int(r2.headers["location"].split("/")[-1])
    time.sleep(0.2)
    with client.websocket_connect(f"/ws/sessions/{sid2}") as ws:
        ws.send_bytes(b"world\n")
        time.sleep(0.3)
    time.sleep(0.3)
    row2 = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid2,))
    assert row2["status"] == "EXITED"  # interactive stdin reached the process, it read and exited
