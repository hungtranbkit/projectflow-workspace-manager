"""Task Wizard: TaskDecisionService.evaluate()['current_step'] is the one
deterministic 'what screen should the user see right now' signal (TASK /
SETUP / AGENT_RUNNING / REVIEW / TEST_QA / INTEGRATION / READY_FOR_MAIN /
DONE), computed from the same status/stage/gate signals evaluate() already
derives -- never a second, template-side lifecycle calculation, and kept
deliberately separate from the simplified Task status (section 23).

These tests exercise the real HTTP API + real git worktrees, deliberately
using sandbox_profile=NONE everywhere so they need no Docker at all (same
no-docker discipline as test_task_lifecycle_engine.py / test_builder_exec_ux.py).

Covers spec section 28's scenarios A-E (F/G, the real tmux/WebSocket
reconnect flow, live in test_web_terminal.py, gated on tmux being
available)."""
from __future__ import annotations
import subprocess

import pytest


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def decision(client, tid):
    return client.get(f"/api/tasks/{tid}/decision").json()


def task(client, tid):
    return client.get(f"/api/tasks/{tid}").json()


def create_and_start(client, title, rid, agent="claude", risk="NORMAL", role=""):
    data = {"title": title, "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": risk}
    if role:
        data["primary_role"] = role
    r = client.post("/api/tasks/create", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


def submit_and_review(client, w, result="PASS"):
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": result}, follow_redirects=False)
    assert r.status_code == 303


def test_a_backlog_task_step_is_task(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    r = client.post("/api/tasks/create", data={"title": "Draft task"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    d = decision(client, tid)
    assert d["status"] == "BACKLOG"
    assert d["current_step"] == "TASK"


def test_a_selected_no_workspace_step_is_setup(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    r = client.post("/api/tasks/create", data={"title": "Draft task"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    client.post(f"/api/tasks/{tid}/select")
    d = decision(client, tid)
    assert d["current_step"] == "SETUP"


def test_a_workspace_created_not_started_step_is_setup(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Simple low-risk task", rid, risk="LOW")
    d = decision(client, tid)
    assert d["current_step"] == "SETUP"
    assert d["builders"][0]["agent_status"] == "NOT_STARTED"


def test_a_low_risk_full_flow_task_setup_builder_review_ready(client, git_repo):
    """A. Simple LOW-risk Task: Task -> Setup -> Builder -> Review -> Ready."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Simple low-risk task", rid, risk="LOW")
    w = task(client, tid)["workspaces"][0]

    submit_and_review(client, w, "PASS")
    d = decision(client, tid)
    assert d["current_step"] == "READY_FOR_MAIN"  # LOW skips QA and Integration entirely
    assert d["status"] == "READY_FOR_MAIN"


def test_b_normal_risk_flow_includes_integration_step(client, git_repo):
    """B. NORMAL Task: Builder -> Review -> Runtime Verification -> Integration -> Ready."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Normal task", rid, risk="NORMAL")
    w = task(client, tid)["workspaces"][0]

    d = decision(client, tid)
    assert d["current_step"] == "SETUP"

    submit_and_review(client, w, "PASS")
    d = decision(client, tid)
    assert d["current_step"] == "TEST_QA"  # NORMAL now requires Runtime Verification too

    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    d = decision(client, tid)
    assert d["current_step"] == "INTEGRATION"

    r = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r.status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    client.app.state.db.execute("UPDATE integration_workspaces SET status='READY_FOR_MAIN',ready_for_main=1 WHERE id=?", (iid,))
    d = decision(client, tid)
    assert d["current_step"] == "READY_FOR_MAIN"


def test_c_high_risk_flow_includes_qa_and_integration_steps(client, git_repo):
    """C. HIGH Task: Builder -> Review -> QA -> Integration -> Ready."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "High risk task", rid, risk="HIGH")
    w = task(client, tid)["workspaces"][0]

    submit_and_review(client, w, "PASS")
    d = decision(client, tid)
    assert d["current_step"] == "TEST_QA"

    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    d = decision(client, tid)
    assert d["current_step"] == "INTEGRATION"

    r = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r.status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    client.app.state.db.execute("UPDATE integration_workspaces SET status='READY_FOR_MAIN',ready_for_main=1 WHERE id=?", (iid,))
    d = decision(client, tid)
    assert d["current_step"] == "READY_FOR_MAIN"


def test_d_review_fix_required_returns_step_to_agent_running(client, git_repo):
    """D. Review FIX_REQUIRED -> returns to Builder (AGENT_RUNNING step)."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Needs fixes", rid, risk="LOW")
    w = task(client, tid)["workspaces"][0]

    submit_and_review(client, w, "FIX_REQUIRED")
    d = decision(client, tid)
    assert d["current_step"] == "AGENT_RUNNING"
    assert d["status"] == "BLOCKED"
    assert d["builders"][0]["fix_required"] is True


def test_d_resume_agent_prompt_includes_review_findings(client, git_repo):
    """D continued (spec section 11, 'Return to Builder'): the resumed
    Builder session's effective prompt is the original task intent PLUS
    the reviewer's findings appended -- not a bare, context-free resume.
    Checked via the same live, freshly-computed workspace_agent_prompt()
    the wizard's Resume Agent action (setup-and-start) snapshots into a
    new session's prompt -- read here through the workspace page's own
    live prompt preview rather than actually starting a session, since
    test settings have no real launcher registered for claude/codex."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Needs fixes", rid, risk="LOW")
    w = task(client, tid)["workspaces"][0]

    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "FIX_REQUIRED", "notes": "Missing input validation on the form handler."})

    prompt = client.get(f"/workspaces/{w['id']}").text
    assert "REVIEW FINDINGS" in prompt
    assert "Missing input validation on the form handler." in prompt
    assert "Needs fixes" in prompt  # original task intent is still there, not replaced


def test_e_multi_builder_waits_for_all_before_review(client, git_repo):
    """E. Multi-builder -> waits for all Builders before advancing to Review."""
    root, repo = git_repo
    other = root / "second"; other.mkdir()
    run(other, "git", "init", "-b", "main"); run(other, "git", "config", "user.email", "t@t"); run(other, "git", "config", "user.name", "t")
    (other / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: second}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (other / "README.md").write_text("x\n"); run(other, "git", "add", "."); run(other, "git", "commit", "-m", "base")
    register(client, repo, "demo"); register(client, other, "second")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    r = client.post("/api/tasks/create", data={
        "title": "Multi-builder task", "risk_profile": "LOW",
        "repository_id": repos["demo"], "agent": "claude", "sandbox_profile": "NONE",
        "ws_repository_id": [str(repos["second"])], "ws_agent": ["codex"], "ws_role": ["Firmware"],
        "ws_base_branch": ["main"], "ws_sandbox_profile": ["NONE"],
    }, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    ws = task(client, tid)["workspaces"]
    assert len(ws) == 2

    submit_and_review(client, ws[0], "PASS")
    d = decision(client, tid)
    assert d["current_step"] == "SETUP"  # second Builder never started -- not REVIEW yet

    submit_and_review(client, ws[1], "PASS")
    d = decision(client, tid)
    assert d["current_step"] == "READY_FOR_MAIN"  # both done, LOW risk needs nothing else
