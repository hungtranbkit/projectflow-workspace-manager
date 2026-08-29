"""Agent Workspace page audit + redesign. Real button trace found three
independent mechanisms conflated on one page: AgentSession (real
pty.fork(), browser xterm terminal, tracked in agent_sessions -- the
one thing "Agent: RUNNING" in the status badge actually means),
TerminalLauncherService (spawns a REAL, separate, untracked codex/
claude process in a desktop GUI terminal window on the HOST's own
graphical session -- completely invisible to agent_status), and a dead
"Open Agent" link that just navigated to a help-page anchor. This
redesign makes AgentSession the one canonical "live agent" flow, backed
by a concurrency guard on the desktop-launcher route, and demotes
verification/report editing to only what the current state warrants."""
from __future__ import annotations
import re
import time

import pytest

from app.launchers import AgentLauncher


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def create_task(client, title, rid, agent="codex", risk="LOW"):
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


def create_task_with_workspace(client, root, repo, name="demo", agent="codex"):
    register(client, repo, name)
    rid = [x for x in client.get("/api/repositories").json() if x["repo_name"] == name][0]["id"]
    tid = create_task(client, "Fix giao dien qa", rid, agent=agent)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    return tid, w


def start_session(client, w, live=True):
    cmd = ("-c", "sleep 100") if live else ("-c", "true")
    client.app.state.agent_sessions.launchers = {w["agent"]: AgentLauncher(w["agent"].capitalize(), "bash", cmd)}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = int(r.headers["location"].split("/")[-1])
    deadline = time.time() + 5
    expected = "RUNNING" if live else "EXITED"
    status = None
    while time.time() < deadline:
        status = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"]
        if status == expected: break
        time.sleep(0.05)
    assert status == expected, f"session never reached {expected} (stuck at {status})"
    return sid


@pytest.fixture(autouse=True)
def _stop_lingering_sessions(client):
    yield
    for row in client.app.state.db.all("SELECT id FROM agent_sessions WHERE status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')"):
        try: client.app.state.agent_sessions.stop(row["id"])
        except Exception: pass


# ------------------------------------------------------------- RUNNING
def test_running_session_hides_start_codex_and_shows_open_live_agent(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=True)
    html = client.get(f"/workspaces/{w['id']}").text
    assert '<button class="success big">Start Codex' not in html
    assert "Open Live Agent" in html
    assert f"/workspaces/{w['id']}/sessions/{sid}" in html


def test_backend_rejects_second_concurrent_desktop_launch(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session(client, w, live=True)
    r = client.post(f"/api/workspaces/{w['id']}/launch-agent")
    assert r.status_code == 409
    assert r.json()["code"] == "ACTIVE_SESSION_EXISTS"


def test_open_worktree_terminal_is_separately_labeled_from_live_agent(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session(client, w, live=True)
    html = client.get(f"/workspaces/{w['id']}").text
    assert "Open Worktree Terminal" in html
    assert "Open Live Agent" in html


def test_no_ambiguous_open_agent_action_anywhere(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    html_idle = client.get(f"/workspaces/{w['id']}").text
    assert "Open Agent" not in html_idle
    start_session(client, w, live=True)
    html_running = client.get(f"/workspaces/{w['id']}").text
    assert "Open Agent" not in html_running


def test_verification_form_hidden_while_running(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session(client, w, live=True)
    html = client.get(f"/workspaces/{w['id']}").text
    assert "Not available yet" in html
    # the raw editable textareas exist in the DOM (collapsed <details>,
    # a deliberate manual-override escape hatch) but are not the
    # dominant, directly-visible content while the agent is live.
    assert "<details id=\"agent-report\">" in html


# ---------------------------------------------------------------- EXITED
def test_exited_resumable_shows_resume_codex(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session(client, w, live=False)
    html = client.get(f"/workspaces/{w['id']}").text
    assert "Resume Codex" in html
    assert '<button class="success big">Start Codex' not in html


def test_no_session_shows_start_codex(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    html = client.get(f"/workspaces/{w['id']}").text
    assert "Start Codex" in html
    assert "Open Live Agent" not in html


# ----------------------------------------------------------------- READY
def test_ready_shows_next_action_progression(client, git_repo):
    """Section 18: once this workspace belongs to a Task, the Current
    Action panel is the SAME user_task_state(decision.evaluate()) Task
    Detail renders -- 'Sẵn sàng review' / 'Start Review' here, never the
    old workspace-only 'Bước tiếp theo' ladder text."""
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={
        "work_status": "READY", "what_changed": "x", "how_to_verify": "1. do a\n2. do b",
    }, follow_redirects=False)
    html = client.get(f"/workspaces/{w['id']}").text
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "SUBMIT_FOR_REVIEW"
    assert "View Task Workflow Summary" in html
    assert d["next_action"]["label"] in html
    assert "do a" in html and "do b" in html


# --------------------------------------------------------------- sandbox
def test_sandbox_not_configured_vs_not_required_are_distinct(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    html = client.get(f"/workspaces/{w['id']}").text
    assert "declares no sandbox contract" in html


# ------------------------------------------------------------ consistency
def test_same_live_session_route_from_workspace_and_live_agents(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=True)
    ws_html = client.get(f"/workspaces/{w['id']}").text
    live_html = client.get("/agents/live").text
    href = f"/workspaces/{w['id']}/sessions/{sid}"
    assert href in ws_html
    assert href in live_html
