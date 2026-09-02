"""P0 (docs/CORE_USABILITY_QUALIFICATION.md failure/recovery matrix:
'DB locked/transient write failure'): reproduced directly with a real
second SQLite connection holding a real EXCLUSIVE write lock for
longer than Database.connect()'s own 10s busy-timeout -- before the
fix, sqlite3.OperationalError escaped completely unhandled (a bare
500/raw exception, indistinguishable from a real code defect, no
'retry' guidance at all). Real timing (~11s), not mocked -- this is a
timing-dependent failure mode and the whole point is proving the
REAL busy-timeout boundary is crossed, not simulating one."""
from __future__ import annotations
import sqlite3
import threading
import time

from app.config import Settings
from tests.conftest import build_client


def test_write_blocked_past_busy_timeout_returns_clean_503_not_a_raw_crash(git_repo, tmp_path):
    root, repo = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    # A real row must already exist for init() to have created the
    # schema before we lock it out from under a fresh write attempt.
    client.get("/")

    blocker = sqlite3.connect(str(settings.db_path), timeout=0.1, check_same_thread=False)
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.execute("CREATE TABLE IF NOT EXISTS _lock_probe(x)")

    release_at = time.monotonic() + 11
    def release_later():
        while time.monotonic() < release_at:
            time.sleep(0.1)
        blocker.commit()
        blocker.close()
    t = threading.Thread(target=release_later, daemon=True)
    t.start()
    try:
        r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})
        assert r.status_code == 503, r.text
        assert "temporarily busy" in r.text.lower() or "temporarily unavailable" in r.text.lower(), r.text
    finally:
        t.join(timeout=15)


def test_api_route_gets_json_503_not_html(git_repo, tmp_path):
    """The same failure through an /api/* route must get the JSON
    shape every other domain-error handler in this app already uses
    for that prefix, never the HTML 'Action blocked' page."""
    root, repo = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})
    rid = client.get("/api/repositories").json()[0]["id"]

    blocker = sqlite3.connect(str(settings.db_path), timeout=0.1, check_same_thread=False)
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.execute("CREATE TABLE IF NOT EXISTS _lock_probe2(x)")

    release_at = time.monotonic() + 11
    def release_later():
        while time.monotonic() < release_at:
            time.sleep(0.1)
        blocker.commit()
        blocker.close()
    t = threading.Thread(target=release_later, daemon=True)
    t.start()
    try:
        r = client.post("/api/tasks", data={"title": "Locked-out task", "repo_scope_id": str(rid)})
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "DATABASE_BUSY"
    finally:
        t.join(timeout=15)
