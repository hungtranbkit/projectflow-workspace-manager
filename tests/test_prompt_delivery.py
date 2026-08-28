"""Start Agent must actually deliver the Builder Prompt (Task #5 demo gap
1-5): a real PTY-backed fake launcher (bash -c ..., same monkeypatch
pattern test_control_plane.py already established) stands in for
codex/claude so these tests never need a real agent CLI, while still
exercising the REAL bracketed-paste delivery, the REAL bounded
readiness wait, and the REAL agent_sessions DB columns."""
from __future__ import annotations
import time

from app.launchers import AgentLauncher


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def create_task(client, title, rid, agent="claude", prompt=""):
    data = {"title": title, "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": "LOW"}
    if prompt:
        data["implementation_prompt"] = prompt
    r = client.post("/api/tasks/create", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


def wait_prompt_status(client, sid, statuses, timeout=5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.app.state.db.one("SELECT * FROM agent_sessions WHERE id=?", (sid,))
        if last and last["prompt_status"] in statuses:
            return last
        time.sleep(0.03)
    return last


def setup_fast_ready_launcher(client, script="echo READY; cat"):
    client.app.state.agent_sessions.launchers = {"claude": AgentLauncher("Claude", "bash", ("-c", script))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1


def test_start_builder_delivers_prompt_automatically(client, git_repo):
    """Process launch + prompt delivery + no manual paste required."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = create_task(client, "Do the thing", rid, agent="claude", prompt="Implement the actual feature.")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    r = client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": w["repository_id"], "agent": "claude"}, follow_redirects=False)
    assert r.status_code == 303

    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert session["status"] == "RUNNING"
    session = wait_prompt_status(client, session["id"], ("DELIVERED", "FAILED"))
    assert session["prompt_status"] == "DELIVERED"
    assert session["prompt_source"] == "TASK"
    assert session["delivered_at"]
    import hashlib
    prompt_row = client.app.state.db.one("SELECT content FROM prompts WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert session["prompt_sha256"] == hashlib.sha256(prompt_row["content"].encode()).hexdigest()


def test_title_fallback_prompt_is_delivered_when_no_implementation_prompt(client, git_repo):
    """Task Title fallback: an empty Implementation Prompt still resolves
    to real, delivered intent -- never a blocked/blank Start."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = create_task(client, "Create demo marker file", rid, agent="claude")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": w["repository_id"], "agent": "claude"})
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    session = wait_prompt_status(client, session["id"], ("DELIVERED", "FAILED"))
    assert session["prompt_status"] == "DELIVERED"
    prompt_row = client.app.state.db.one("SELECT content FROM prompts WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert "Create demo marker file" in prompt_row["content"]


def test_delivery_failure_then_manual_retry_succeeds(client, git_repo):
    """CLI readiness detection (section 3): a bounded wait, not a blind
    immediate write. A CLI that stays quiet past the (short, test-tuned)
    timeout gets prompt_status FAILED, never a garbled send; the
    explicit [Retry Prompt Delivery] action then succeeds once the CLI
    has actually settled."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.app.state.agent_sessions.launchers = {"claude": AgentLauncher("Claude", "bash", ("-c", "sleep 1; echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 0.3  # too short for the 1s sleep -> first attempt fails
    client.app.state.agent_sessions.prompt_quiet_window = 0.1
    tid = create_task(client, "Slow starting agent", rid, agent="claude", prompt="Do work.")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": w["repository_id"], "agent": "claude"})
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    session = wait_prompt_status(client, session["id"], ("DELIVERED", "FAILED"))
    assert session["prompt_status"] == "FAILED"

    client.app.state.agent_sessions.prompt_ready_timeout = 3.0  # now give the retry enough time to see it settle
    r = client.post(f"/api/sessions/{session['id']}/deliver-prompt", follow_redirects=False)
    assert r.status_code == 303
    session = wait_prompt_status(client, session["id"], ("DELIVERED",), timeout=5)
    assert session["prompt_status"] == "DELIVERED"


def test_resume_on_already_delivered_session_never_resends(client, git_repo):
    """Resume Agent case B (section 4): a live session whose prompt was
    already DELIVERED must never be sent the prompt a second time, and
    must never spawn a duplicate session/process."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = create_task(client, "Only once", rid, agent="claude", prompt="Do the work exactly once.")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": w["repository_id"], "agent": "claude"})
    first = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    first = wait_prompt_status(client, first["id"], ("DELIVERED", "FAILED"))
    assert first["prompt_status"] == "DELIVERED"
    delivered_at_1, sha_1, pid_1 = first["delivered_at"], first["prompt_sha256"], first["pid"]

    # Press "Resume Agent" (setup-and-start on the same repo+agent) again --
    # this must be a no-op: same session id, same delivered_at, no second row.
    client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": w["repository_id"], "agent": "claude"})
    time.sleep(0.2)
    rows = client.app.state.db.all("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id", (w["id"],))
    assert len(rows) == 1, "a second session row was created -- the original task was resent as a duplicate session"
    assert rows[0]["id"] == first["id"]
    assert rows[0]["pid"] == pid_1
    assert rows[0]["delivered_at"] == delivered_at_1
    assert rows[0]["prompt_sha256"] == sha_1


def test_return_to_builder_delivers_repair_prompt_with_review_findings(client, git_repo):
    """Resume Agent case C (section 4): a FIX_REQUIRED review makes the
    NEXT delivered prompt a repair prompt (original task + findings),
    never a bare resend of the original task."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = create_task(client, "Needs fixes", rid, agent="claude", prompt="Implement the feature.")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "FIX_REQUIRED", "notes": "Add missing null check."})

    client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": w["repository_id"], "agent": "claude"})
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    session = wait_prompt_status(client, session["id"], ("DELIVERED", "FAILED"))
    assert session["prompt_status"] == "DELIVERED"
    assert session["prompt_source"] == "REPAIR"
    prompt_row = client.app.state.db.one("SELECT content FROM prompts WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert "Add missing null check." in prompt_row["content"]
    assert "Needs fixes" in prompt_row["content"]
