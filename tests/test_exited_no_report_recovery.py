"""Real bug: AgentSession EXITED with no persisted completion report was a
workflow dead-end -- the only actions offered were Resume Agent / Mark
Blocked, even when the source work was genuinely already complete and
committed. AgentSession EXITED never by itself means the Builder failed
(it can just as easily mean the process exited after finishing, before a
report ever got persisted). This adds a manual "Mark Ready for Review"
recovery fallback, gated on real git-worktree validation (never a blind
force-pass), plus a focused resume prompt so Resume stays the preferred
path when the user wants the agent to write the report itself."""
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


# No sleep -- these scripts must actually exit so the session reaches EXITED.
COMMIT_NO_REPORT_SCRIPT = "echo real-work >> newfile.txt && git add newfile.txt && git -c user.email=a@b.c -c user.name=agent commit -q -m 'agent work' && echo done"
DIRTY_NO_REPORT_SCRIPT = "echo uncommitted >> newfile.txt && echo done"
NO_CHANGE_SCRIPT = "echo done"
LOOSE_TEXT_SCRIPT = "printf 'I think this looks READY to me, still double-checking things though\\n'; echo done"
REAL_REPORT_SCRIPT = r"echo real-work >> newfile.txt && git add newfile.txt && git -c user.email=a@b.c -c user.name=agent commit -q -m 'agent work' && printf 'WORK_STATUS:\nREADY\n\nWHAT_CHANGED:\nReal change committed.\n\nHOW_TO_VERIFY:\n1. Check newfile.txt\n\nEXPECTED_RESULT:\nFile exists\n\nRISKS:\nnone\n'"


def run_session_to_exit(client, w, script):
    client.app.state.agent_sessions.launchers = {w["agent"]: AgentLauncher(w["agent"].capitalize(), "bash", ("-c", script))}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = int(r.headers["location"].split("/")[-1])
    deadline = time.time() + 8
    status = None
    while time.time() < deadline:
        status = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"]
        if status == "EXITED":
            break
        time.sleep(0.05)
    assert status == "EXITED", f"session never reached EXITED (status={status})"
    return sid


@pytest.fixture(autouse=True)
def _stop_lingering_sessions(client):
    yield
    for row in client.app.state.db.all("SELECT id FROM agent_sessions WHERE status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')"):
        try: client.app.state.agent_sessions.stop(row["id"])
        except Exception: pass


def find_mark_ready_form(html):
    m = re.search(r'<form method="post" action="(/api/workspaces/\d+/mark-ready-manual)"[^>]*>(.*?)</form>', html, re.DOTALL)
    return m


# --------------------------------------------------------- validation gate
def test_exited_clean_committed_source_offers_mark_ready(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert "did not receive a completion report" in html
    assert find_mark_ready_form(html), "Mark Ready for Review form should be offered for clean committed source"


def test_exited_dirty_source_blocks_mark_ready(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, DIRTY_NO_REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert not find_mark_ready_form(html), "Mark Ready for Review must not be offered with uncommitted changes"
    assert "UNCOMMITTED_CHANGES" not in html  # code is internal; detail text is what's shown
    assert "Worktree" in html or "chưa commit" in html


def test_exited_no_source_changes_blocks_mark_ready(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, NO_CHANGE_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert not find_mark_ready_form(html), "Mark Ready for Review must not be offered with zero source changes"


def test_backend_reverifies_even_if_client_state_stale(client, git_repo):
    """Section 12: the route itself re-validates -- posting directly (as
    if a stale page were used) to mark-ready-manual on a dirty worktree
    must still be blocked server-side, never trust the button was only
    shown when valid."""
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, DIRTY_NO_REPORT_SCRIPT)
    r = client.post(f"/api/workspaces/{w['id']}/mark-ready-manual",
                     data={"what_changed": "x", "how_to_verify": "y"}, follow_redirects=False)
    assert r.status_code >= 400
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["status"] != "READY"


# -------------------------------------------------------- manual ready flow
def test_manual_ready_pins_exact_head_and_advances_wizard_to_review(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    real_head = client.app.state.git.head(w["worktree_path"])
    r = client.post(f"/api/workspaces/{w['id']}/mark-ready-manual",
                     data={"what_changed": "Added newfile.txt", "how_to_verify": "1. Check newfile.txt exists"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["stage"] == "REVIEW"
    builder = d["builders"][0]
    assert builder["ready"] is True
    assert builder["agent_status"] == "EXITED"  # session state untouched

    report = client.app.state.db.one("SELECT * FROM verification_reports WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert report["commit_sha"] == real_head
    assert report["ready_source"] == "MANUAL_CONFIRMATION"
    assert report["operator"] == "ui"
    assert report["what_changed"] == "Added newfile.txt"


def test_manual_ready_requires_what_changed_and_how_to_verify(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    r = client.post(f"/api/workspaces/{w['id']}/mark-ready-manual", data={"what_changed": "", "how_to_verify": ""}, follow_redirects=False)
    assert r.status_code >= 400
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["status"] != "READY"


def test_manual_ready_defaults_tests_run_and_risks(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    r = client.post(f"/api/workspaces/{w['id']}/mark-ready-manual",
                     data={"what_changed": "x", "how_to_verify": "y"}, follow_redirects=False)
    assert r.status_code == 303
    report = client.app.state.db.one("SELECT * FROM verification_reports WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert report["tests_run"] == "Not run"
    assert report["risks"] == "None known"


# --------------------------------------------------------------- resume
def test_resume_remains_available_after_exit(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert "Resume Codex" in html


def test_resume_sends_focused_prompt_about_missing_report(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid1 = run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    # Resume launches a fresh session (the exited one can't take new input);
    # capture the prompt actually delivered/recorded for it.
    client.app.state.agent_sessions.launchers = {w["agent"]: AgentLauncher(w["agent"].capitalize(), "bash", ("-c", "sleep 100"))}
    r = client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": str(w["repository_id"]), "agent": w["agent"]}, follow_redirects=False)
    assert r.status_code == 303, r.text
    prompt_row = client.app.state.db.one("SELECT content FROM prompts WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert "previous session exited without a persisted completion report" in prompt_row["content"]
    assert "Do not redo completed work unnecessarily" in prompt_row["content"]


def test_resume_does_not_start_a_duplicate_concurrent_session(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    client.app.state.agent_sessions.launchers = {w["agent"]: AgentLauncher(w["agent"].capitalize(), "bash", ("-c", "sleep 100"))}
    r1 = client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": str(w["repository_id"]), "agent": w["agent"]}, follow_redirects=False)
    assert r1.status_code == 303
    live = [x for x in client.app.state.db.all("SELECT id,status FROM agent_sessions WHERE workspace_id=?", (w["id"],)) if x["status"] in ("STARTING", "RUNNING", "WAITING_FOR_INPUT")]
    assert len(live) == 1
    # A second resume click while it's already live must not fork another one.
    r2 = client.post(f"/api/tasks/{tid}/setup-and-start", data={"repository_id": str(w["repository_id"]), "agent": w["agent"]}, follow_redirects=False)
    assert r2.status_code == 303
    live2 = [x for x in client.app.state.db.all("SELECT id,status FROM agent_sessions WHERE workspace_id=?", (w["id"],)) if x["status"] in ("STARTING", "RUNNING", "WAITING_FOR_INPUT")]
    assert len(live2) == 1
    assert live2[0]["id"] == live[0]["id"]


# ---------------------------------------------------- auto-recovery (7)
def test_deterministic_completion_output_auto_recovers_even_after_exit(client, git_repo):
    """Section 7: a real, well-formed report sitting in the exited
    session's own transcript (persisted on exit, see the transcript-tail
    fix) is detected and offered as a one-click confirm -- never silently
    auto-applied, but the user is never sent to the manual-form dead-end
    when real evidence already exists."""
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, REAL_REPORT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert "completion report detected" in html
    assert find_mark_ready_form(html) is None, "manual fallback must not appear when a real detected report already covers it"
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["status"] != "READY"  # still requires a human confirm click, never auto-applied


def test_loose_terminal_text_does_not_falsely_trigger_auto_ready(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, LOOSE_TEXT_SCRIPT)
    html = client.get(f"/tasks/{tid}").text
    assert "completion report detected" not in html
    # No source changes were committed either, so Mark Ready stays blocked too.
    assert find_mark_ready_form(html) is None


# --------------------------------------------------- EXITED != failed (8)
def test_exited_does_not_automatically_mark_builder_failed(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, NO_CHANGE_SCRIPT)
    w2 = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    assert w2["status"] not in ("FAILED", "BLOCKED")
    d = client.get(f"/api/tasks/{tid}/decision").json()
    builder = d["builders"][0]
    assert builder["fix_required"] is False


# --------------------------------------------------------------- audit
def test_manual_ready_success_writes_audit_events(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    client.post(f"/api/workspaces/{w['id']}/mark-ready-manual", data={"what_changed": "x", "how_to_verify": "y"}, follow_redirects=False)
    actions = [e["action"] for e in client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_id=? ORDER BY id", (w["id"],))]
    assert "BUILDER_MANUAL_READY_REQUESTED" in actions
    assert "BUILDER_MANUAL_READY_SUCCEEDED" in actions
    assert "BUILDER_MANUAL_READY_BLOCKED" not in actions


def test_manual_ready_blocked_writes_audit_event(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, DIRTY_NO_REPORT_SCRIPT)
    client.post(f"/api/workspaces/{w['id']}/mark-ready-manual", data={"what_changed": "x", "how_to_verify": "y"}, follow_redirects=False)
    actions = [e["action"] for e in client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_id=? ORDER BY id", (w["id"],))]
    assert "BUILDER_MANUAL_READY_REQUESTED" in actions
    assert "BUILDER_MANUAL_READY_BLOCKED" in actions
    assert "BUILDER_MANUAL_READY_SUCCEEDED" not in actions


# ---------------------------------------------------- page refresh (14)
def test_page_refresh_preserves_ready_state(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    run_session_to_exit(client, w, COMMIT_NO_REPORT_SCRIPT)
    client.post(f"/api/workspaces/{w['id']}/mark-ready-manual", data={"what_changed": "x", "how_to_verify": "y"}, follow_redirects=False)
    for _ in range(2):
        html = client.get(f"/tasks/{tid}").text
        assert "READY" in html or "Review" in html
        d = client.get(f"/api/tasks/{tid}/decision").json()
        assert d["stage"] == "REVIEW"
