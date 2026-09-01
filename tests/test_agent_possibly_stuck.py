"""P0-3 (docs/CORE_USABILITY_QUALIFICATION.md): a session can be
genuinely alive (real PID, real PTY, status RUNNING) while producing
zero output for a long time -- indistinguishable from a real hang by
`status` alone. `session_possibly_stuck()` surfaces this heuristically
on /agents/live and the Task detail page. Real PTY session throughout
(the same `sleep 100` pattern test_live_terminal_routing.py already
established), only `last_activity_at` is time-travelled backward via a
real DB update (the only practical way to prove a 3-minute-old
timestamp without a real 3-minute sleep)."""
from __future__ import annotations
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_live_terminal_routing import create_task_with_workspace, start_session


@pytest.fixture(autouse=True)
def _stop_lingering_sessions(client):
    yield
    for row in client.app.state.db.all("SELECT id FROM agent_sessions WHERE status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')"):
        try: client.app.state.agent_sessions.stop(row["id"])
        except Exception: pass


def test_freshly_active_session_not_flagged_stuck(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    start_session(client, w, live=True)

    r = client.get("/agents/live")
    assert r.status_code == 200
    assert "may be stuck" not in r.text

    r = client.get(f"/tasks/{tid}")
    assert r.status_code == 200
    assert "may be stuck" not in r.text


def test_stale_running_session_flagged_possibly_stuck(client, git_repo):
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=True)

    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    client.app.state.db.execute("UPDATE agent_sessions SET last_activity_at=? WHERE id=?", (stale, sid))

    r = client.get("/agents/live")
    assert r.status_code == 200
    assert "may be stuck" in r.text

    r = client.get(f"/tasks/{tid}")
    assert r.status_code == 200
    assert "may be stuck" in r.text


def test_exited_session_never_flagged_stuck_even_if_old(client, git_repo):
    """A session that has genuinely finished (EXITED/FAILED) must never
    be labeled 'possibly stuck' -- that label is only meaningful for a
    session still claiming to be live."""
    root, repo = git_repo
    tid, w = create_task_with_workspace(client, root, repo)
    sid = start_session(client, w, live=False)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    client.app.state.db.execute("UPDATE agent_sessions SET last_activity_at=? WHERE id=?", (stale, sid))

    r = client.get(f"/tasks/{tid}")
    assert r.status_code == 200
    assert "may be stuck" not in r.text
