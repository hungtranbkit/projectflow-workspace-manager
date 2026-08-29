"""'View Live Terminal' on Task pages used to link to /workspaces/<id> --
the Agent Workspace management page, not the actual PTY-backed terminal
route (/workspaces/<id>/sessions/<sid>). Real bug report: a RUNNING
session with a DELIVERED prompt still showed a primary action that
opened the wrong page. This fixes the routing (from the authoritative
AgentSession, never guessed) and separates it from a genuinely distinct
secondary action, "View Workspace Details".

A real PTY session is created via the real /api/workspaces/{id}/sessions
route (real fork/exec, real agent_sessions row). For a RUNNING scenario
the child runs `sleep 100` -- long enough to stay genuinely alive for
the whole test, no status faking needed (a `read` waiting on stdin
looked appealing but is racy here: this sandbox's PTY delivers EOF to
it almost immediately with no controlling terminal attached, and
AgentSessionManager's own background reaper thread then flips the row
to EXITED asynchronously, at a timing this test can't control -- the
same race test_control_plane.py's test_pty_session_lifecycle_and_ownership
already tolerates by accepting either RUNNING or EXITED). For an EXITED
scenario the child just exits immediately (`true`), which is what
naturally happens anyway -- no faking there either."""
from __future__ import annotations
import re
import time

import pytest

from app.launchers import AgentLauncher


@pytest.fixture(autouse=True)
def _stop_lingering_sessions(client):
    yield
    for row in client.app.state.db.all("SELECT id FROM agent_sessions WHERE status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')"):
        try: client.app.state.agent_sessions.stop(row["id"])
        except Exception: pass


def create_task_with_workspace(client, root, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    rid = [x for x in client.get("/api/repositories").json() if x["repo_name"] == name][0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Fix giao dien qa", "repository_id": rid, "agent": "codex", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    return tid, w


def start_session(client, w, live=True):
    """A real AgentSession row via the real route (real fork/exec, real
    ownership). `live=True` keeps a real process running (sleep 100) for
    the whole test; `live=False` lets it exit immediately for real."""
    cmd = ("-c", "sleep 100") if live else ("-c", "true")
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", cmd)}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = int(r.headers["location"].split("/")[-1])
    client.app.state.db.execute("UPDATE agent_sessions SET prompt_status='DELIVERED' WHERE id=?", (sid,))
    deadline = time.time() + 5
    expected = "RUNNING" if live else "EXITED"
    while time.time() < deadline:
        status = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"]
        if status == expected: break
        time.sleep(0.05)
    assert status == expected, f"session never reached {expected} (stuck at {status})"
    return sid


TERMINAL_HREF = re.compile(r'href="(/workspaces/\d+/sessions/\d+)"')
WRONG_HREF_AS_PRIMARY = re.compile(r'<a class="button" href="/workspaces/\d+">')


def test_running_session_open_live_terminal_points_to_session_route(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=True)
    html = client.get(f"/tasks/{tid}").text
    assert f"/workspaces/{w['id']}/sessions/{sid}" in html
    assert "Open Live Terminal" in html


def test_open_live_terminal_does_not_point_to_workspace_id_alone(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session(client, w, live=True)
    html = client.get(f"/tasks/{tid}").text
    assert not WRONG_HREF_AS_PRIMARY.search(html), "primary action still links bare /workspaces/<id>"


def test_workspace_link_labeled_view_workspace_details(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session(client, w, live=True)
    html = client.get(f"/tasks/{tid}").text
    assert "View Workspace Details" in html
    assert f'href="/workspaces/{w["id"]}"' in html


def test_ready_builder_without_session_does_not_show_terminal_action(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    # No session started yet at all.
    html = client.get(f"/tasks/{tid}").text
    assert "Open Live Terminal" not in html


def test_exited_session_does_not_show_active_terminal_action(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=False)
    html = client.get(f"/tasks/{tid}").text
    assert "Open Live Terminal" not in html
    # Still resolvable to review what happened, just not framed as "live".
    assert "View Session" in html or "View Workspace Details" in html


def test_same_session_route_used_from_task_and_live_agents(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=True)
    task_html = client.get(f"/tasks/{tid}").text
    live_html = client.get("/agents/live").text
    task_hrefs = set(TERMINAL_HREF.findall(task_html))
    live_hrefs = set(TERMINAL_HREF.findall(live_html))
    expected = f"/workspaces/{w['id']}/sessions/{sid}"
    assert expected in task_hrefs
    assert expected in live_hrefs


def test_no_empty_advanced_headings_or_cards(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    html = client.get(f"/tasks/{tid}").text
    assert not re.search(r"<h[1-6]>\s*</h[1-6]>", html)
    assert not re.search(r"<p><small>[^<]*</small></p>\s*(?:</div>|</section>|<h)", html)


def test_session_detail_page_shows_task_agent_repository_branch_status(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=True)
    html = client.get(f"/workspaces/{w['id']}/sessions/{sid}").text
    assert "Fix giao dien qa" in html
    assert "codex" in html.lower()
    assert w["repo_name"] in html
    assert w["branch"] in html
    assert "RUNNING" in html
