"""AgentSession lifecycle and Builder workflow completion are different
things (the spec this implements). Real bug: Codex prints its own
completion report to its own terminal and returns to an interactive
prompt -- the process (AgentSession) stays RUNNING, but there is no API
callback wired up for the agent to call, so nothing ever submits that
report as a real VerificationReport, and the Task stays stuck at
AGENT_RUNNING even though the source work is genuinely done. This adds
conservative, never-auto-submitted detection of a well-formed report in
the session's own live transcript, offered as a one-click confirm --
the human still presses the button, same as pasting the report by hand,
just without the copy/paste."""
from __future__ import annotations
import re
import time

import pytest

from app.launchers import AgentLauncher


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def create_task_with_workspace(client, root, repo, name="demo", agent="codex"):
    register(client, repo, name)
    rid = [x for x in client.get("/api/repositories").json() if x["repo_name"] == name][0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Fix giao dien qa", "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    return tid, w


REPORT_SCRIPT = r"""printf 'WORK_STATUS:\nREADY\n\nWHAT_CHANGED:\nStandardized the QA Center operations console.\n\nAUTOMATED_TESTS:\nran pytest, all passed\n\nHOW_TO_VERIFY:\n1. Open QA Center\n2. Check the console\n\nEXPECTED_RESULT:\nConsole renders consistently\n\nTEST_DATA:\nnone\n\nRUNTIME_REQUIREMENTS:\nNONE\n\nRISKS:\nnone\n'; sleep 100"""

FIX_REQUIRED_SCRIPT = r"""printf 'WORK_STATUS:\nFIX_REQUIRED\n\nWHAT_CHANGED:\nStill working on it, tests fail.\n'; sleep 100"""

NO_REPORT_SCRIPT = "sleep 100"


def start_session_with_output(client, w, script):
    client.app.state.agent_sessions.launchers = {w["agent"]: AgentLauncher(w["agent"].capitalize(), "bash", ("-c", script))}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = int(r.headers["location"].split("/")[-1])
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"] == "RUNNING":
            break
        time.sleep(0.05)
    # give the script a moment to actually flush its printf into the buffer
    deadline2 = time.time() + 3
    while time.time() < deadline2:
        if client.app.state.agent_sessions.live_tail(sid):
            break
        time.sleep(0.05)
    client.app.state.db.execute("UPDATE agent_sessions SET prompt_status='DELIVERED' WHERE id=?", (sid,))
    return sid


@pytest.fixture(autouse=True)
def _stop_lingering_sessions(client):
    yield
    for row in client.app.state.db.all("SELECT id FROM agent_sessions WHERE status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')"):
        try: client.app.state.agent_sessions.stop(row["id"])
        except Exception: pass


# ------------------------------------------------------------- detection
def test_running_session_with_no_report_stays_agent_running(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session_with_output(client, w, NO_REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert "completion report detected" not in html
    assert "Open Live Terminal" in html


def test_running_session_with_valid_report_shows_confirm_action(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session_with_output(client, w, REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert "completion report detected" in html
    assert "Confirm &amp; Submit for Review" in html
    assert "Standardized the QA Center operations console." in html
    # never silently submitted -- workspace status must still be CREATED
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["status"] != "READY"


def test_confirm_click_submits_report_and_advances_to_review(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session_with_output(client, w, REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    m = re.search(r'<form method="post" action="(/api/workspaces/\d+/verification-report)"[^>]*>(.*?)</form>', html, re.DOTALL)
    assert m, "confirm form not found"
    action, body = m.groups()
    fields = dict(re.findall(r'name="([\w]+)" value="([^"]*)"', body))
    r = client.post(action, data=fields, follow_redirects=False)
    assert r.status_code == 303, r.text

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["stage"] == "REVIEW"
    builder = d["builders"][0]
    assert builder["ready"] is True
    assert builder["agent_status"] == "RUNNING"  # session was never touched


def test_session_stays_running_after_confirm(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session_with_output(client, w, REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    m = re.search(r'<form method="post" action="(/api/workspaces/\d+/verification-report)"[^>]*>(.*?)</form>', html, re.DOTALL)
    action, body = m.groups()
    fields = dict(re.findall(r'name="([\w]+)" value="([^"]*)"', body))
    client.post(action, data=fields, follow_redirects=False)
    assert client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"] == "RUNNING"


def test_fix_required_report_offers_confirm_but_not_ready_label(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session_with_output(client, w, FIX_REQUIRED_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert "completion report detected" in html
    assert "Confirm (Fix Required)" in html


# -------------------------------------------------------- stop semantics
def test_stop_does_not_mark_incomplete_builder_ready(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session_with_output(client, w, NO_REPORT_SCRIPT)
    client.post(f"/api/sessions/{sid}/stop", follow_redirects=False)
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["status"] != "READY"


def test_stop_after_confirm_preserves_ready_for_review(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session_with_output(client, w, REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    m = re.search(r'<form method="post" action="(/api/workspaces/\d+/verification-report)"[^>]*>(.*?)</form>', html, re.DOTALL)
    action, body = m.groups()
    fields = dict(re.findall(r'name="([\w]+)" value="([^"]*)"', body))
    client.post(action, data=fields, follow_redirects=False)
    client.post(f"/api/sessions/{sid}/stop", follow_redirects=False)
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["stage"] == "REVIEW"
    assert d["builders"][0]["ready"] is True


# --------------------------------------------------------- concurrency
def test_start_codex_blocked_while_session_running_even_after_ready(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session_with_output(client, w, REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    m = re.search(r'<form method="post" action="(/api/workspaces/\d+/verification-report)"[^>]*>(.*?)</form>', html, re.DOTALL)
    action, body = m.groups()
    fields = dict(re.findall(r'name="([\w]+)" value="([^"]*)"', body))
    client.post(action, data=fields, follow_redirects=False)
    r = client.post(f"/api/workspaces/{w['id']}/launch-agent")
    assert r.status_code == 409
    assert r.json()["code"] == "ACTIVE_SESSION_EXISTS"


# ------------------------------------------------------------- parser
def test_parser_ignores_stray_ready_word():
    from app.services.completion_report_parser import parse_completion_report
    assert parse_completion_report("I think this is READY to look at, still coding though") is None


def test_parser_rejects_incomplete_block():
    from app.services.completion_report_parser import parse_completion_report
    assert parse_completion_report("WORK_STATUS:\nREADY\n") is None  # no WHAT_CHANGED


def test_ansi_stripped_from_activity_summary(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session_with_output(client, w, r"printf '\x1b[31mHello\x1b[0m world\n'; sleep 100")
    html = client.get(f"/tasks/{tid}").text
    assert "\x1b[" not in html
