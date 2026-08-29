"""Real bug, found via live production verification of PR #25: a real
Codex TUI redraws its whole screen (cursor moves, spinner frames, style
resets) for every printed line, so a completion report's WORK_STATUS
marker can sit tens of thousands of raw bytes before its WHAT_CHANGED
marker even though both are only a few lines apart in the *clean* text.
persist_tail() used to downsample the live session's own 200_000-byte
buffer down to a fixed 20_000-byte tail on every WS disconnect --
independent of, and much smaller than, the live buffer's own cap -- so a
report that was fully visible in the live terminal could still vanish
the moment nobody was connected to see it. persist_tail() was also only
ever called from the WebSocket route's disconnect handler, so a session
that exited (or that the server killed on a restart) while nobody had
the live terminal open lost its transcript entirely.

This test proves both are fixed: a session that pads well past the old
20_000-byte cap between WORK_STATUS and WHAT_CHANGED, and that EXITS on
its own (never had a WS connection open), still yields a correctly
parsed completion report from live_tail() after it's gone."""
from __future__ import annotations
import time

from app.launchers import AgentLauncher
from app.services.completion_report_parser import parse_completion_report


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def create_task_with_workspace(client, root, repo, name="demo", agent="codex"):
    register(client, repo, name)
    rid = [x for x in client.get("/api/repositories").json() if x["repo_name"] == name][0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Long report", "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    return tid, w


# A real Codex-style report: WORK_STATUS up front, then >25_000 RAW bytes
# of ANSI cursor/erase-line noise standing in for TUI redraw traffic --
# it fully disappears after strip_ansi() (real report content stays
# close together in the *clean* text, same as production; only the raw
# byte distance is large) -- well past the old 20_000-byte persisted-tail
# cap and comfortably under the 200_000-byte live buffer cap, then
# WHAT_CHANGED and a normal exit (no sleep -- proves the on_exit path,
# not just the WS-disconnect path).
LONG_REPORT_SCRIPT = (
    "printf 'WORK_STATUS:\\nREADY\\n\\n'; "
    "python3 -c \"import sys; sys.stdout.write('\\x1b[2K\\x1b[0m' * 5000)\"; "
    "printf '\\nWHAT_CHANGED:\\nStandardized the console.\\n\\nRISKS:\\nnone\\n'"
)


def test_report_with_marker_far_apart_survives_process_exit(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    client.app.state.agent_sessions.launchers = {w["agent"]: AgentLauncher(w["agent"].capitalize(), "bash", ("-c", LONG_REPORT_SCRIPT))}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = int(r.headers["location"].split("/")[-1])

    # Never open the live terminal / WS at all -- wait for the process to
    # exit entirely on its own, exactly like a session nobody was
    # watching when the agent finished.
    deadline = time.time() + 8
    status = None
    while time.time() < deadline:
        status = client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"]
        if status == "EXITED":
            break
        time.sleep(0.05)
    assert status == "EXITED", f"session never exited on its own (status={status})"

    tail = client.app.state.agent_sessions.live_tail(sid)
    assert tail is not None
    assert len(tail) > 25000, "persisted tail was re-truncated below the live buffer's own cap"
    report = parse_completion_report(tail)
    assert report is not None, "WORK_STATUS marker was lost even though it's within the live buffer's own cap"
    assert report["WORK_STATUS"] == "READY"
    assert report["WHAT_CHANGED"] == "Standardized the console."


def test_persist_tail_keeps_full_live_buffer_not_a_smaller_window(client, git_repo):
    """persist_tail() must not re-truncate below the live session's own
    BUFFER_CAP -- that was the actual bug (a 20_000 re-truncation of an
    already-200_000-capped buffer)."""
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    client.app.state.agent_sessions.launchers = {w["agent"]: AgentLauncher(w["agent"].capitalize(), "bash", ("-c", "python3 -c \"print('y'*30000)\"; sleep 100"))}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = int(r.headers["location"].split("/")[-1])
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.app.state.agent_sessions.live_tail(sid) and len(client.app.state.agent_sessions.live_tail(sid)) > 25000:
            break
        time.sleep(0.05)
    client.app.state.agent_sessions.persist_tail(sid)
    row = client.app.state.db.one("SELECT transcript_tail FROM agent_sessions WHERE id=?", (sid,))
    assert len(row["transcript_tail"]) > 25000, "persist_tail re-truncated the buffer to a smaller window"
    client.app.state.agent_sessions.stop(sid)
