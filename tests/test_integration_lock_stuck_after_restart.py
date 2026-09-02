"""P0 BLOCKER (docs/CORE_USABILITY_QUALIFICATION.md, final stability
pass): the most severe defect found during this program's repo-wide
audit for stuck-forever busy state. repository_integration_locks.
repository_id is a real PRIMARY KEY -- IntegrationService's own atomic
single-writer lock for "an integration is in progress for this
repository right now" (_lock()/_unlock(), held across integrate_task()
via try/finally). A Python `finally` block only protects against an
in-process exception -- it does NOT run if the process itself is
killed/crashes/restarts mid-integration. Before this fix, a stale row
left by a hard kill made that EXACT repository permanently unable to
integrate anything, ever again -- confirmed real via a direct
_lock()/_lock() reproduction. IntegrationService.reconcile_on_startup()
(wired at app startup) now clears every row unconditionally."""
from __future__ import annotations

from app.config import Settings
from tests.conftest import build_client
from tests.test_integration_push import register


def test_stale_integration_lock_blocks_every_future_integration_before_reconcile(client, git_repo):
    """First prove the blast radius directly with the real service's
    own _lock() method (the exact mechanism integrate_task() itself
    calls, see its own try/finally) -- a stale lock left by a crashed
    process really does make every subsequent lock acquisition for
    that repository fail."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    db = client.app.state.db

    # Simulate a hard kill mid-integration: the INSERT half of the
    # lock/work/unlock sequence ran, the finally-block's _unlock()
    # never got to run because the process itself died, not because a
    # Python exception was raised.
    db.execute("INSERT INTO repository_integration_locks(repository_id,locked_by) VALUES(?,?)", (rid, "task:999"))

    svc = client.app.state.integration_service
    assert svc._lock(rid, "task:1000") is False, \
        "a stale lock from a dead process must be indistinguishable from a real in-progress one -- confirms the blast radius"


def test_restart_reconciliation_clears_stale_lock_and_integration_works_again(git_repo, tmp_path):
    """The real fix, proven with a real restart: a fresh create_app()
    against the SAME DB (simulating the old process's own death) must
    clear the stale lock via reconcile_on_startup() alone, and the
    exact same repository's integration must actually run again
    afterward."""
    root, repo = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]

    db = client.app.state.db
    db.execute("INSERT INTO repository_integration_locks(repository_id,locked_by) VALUES(?,?)", (rid, "task:999"))
    assert db.one("SELECT * FROM repository_integration_locks WHERE repository_id=?", (rid,)), \
        "sanity: the stale lock is really there"

    # A genuine restart: a brand-new create_app() against the same DB
    # file -- integration_service.reconcile_on_startup() runs exactly
    # as it would in production.
    restarted = build_client(settings)

    assert not restarted.app.state.db.one("SELECT * FROM repository_integration_locks WHERE repository_id=?", (rid,)), \
        "stale integration lock must be cleared by restart reconciliation"

    # The exact real mechanism integrate_task() itself relies on --
    # acquiring the lock must actually work again, not stay
    # permanently blocked.
    assert restarted.app.state.integration_service._lock(rid, "task:1000") is True, \
        "the repository must be lockable again after restart reconciliation, not permanently blocked"
