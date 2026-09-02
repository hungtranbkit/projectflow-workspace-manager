"""P0 (docs/CORE_USABILITY_QUALIFICATION.md, final stability pass): a
REAL, reproduced defect found during a repo-wide audit for stuck-
forever busy state. task_reservations.task_id is a real PRIMARY KEY --
ExecutionWaveService's own atomic single-writer lock for "this Task is
being launched into a wave right now". Its own module docstring states
reservations are short-lived, released the moment a launch succeeds or
fails -- but that reasoning only covers the launch's own two LOGICAL
outcomes, not the process itself dying between the INSERT
(run_execution_wave()) and the matching DELETE a few lines later in
the SAME call. Before this fix, a row surviving to the next process
start made that exact Task permanently unreservable by every future
wave-scheduling attempt, forever -- confirmed real via a direct
UNIQUE-constraint reproduction. ExecutionWaveService.reconcile_on_
startup() (wired at app startup) now clears every row unconditionally
(every row in this table is inherently transient; there is no
"reservation that should legitimately survive a restart" case)."""
from __future__ import annotations

from app.config import Settings
from tests.conftest import build_client
from tests.test_execution_waves import enable_parallel, fake_launcher, new_plan_task
from tests.test_autonomous_execution import register, new_change


def test_stale_reservation_from_a_dead_process_blocks_scheduling_before_reconcile(client, git_repo):
    """First prove the blast radius directly: a stale reservation left
    by a crashed process really does make the exact same INSERT the
    real scheduler performs fail with a UNIQUE-constraint violation."""
    root, repo = git_repo
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Stuck reservation change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])

    db = client.app.state.db
    # Simulate a crashed prior process: the INSERT half of the atomic
    # reserve/launch/release sequence ran, the DELETE never did.
    db.execute("INSERT INTO task_reservations(task_id,wave_id,state) VALUES(?,?,?)", (a, None, "RESERVED"))

    fake_launcher(client, "claude")
    run = client.app.state.execution_wave_service.run_execution_wave(cid)
    assert run["outcome"] == "NO_TASKS_LAUNCHED", run
    wt = db.one("SELECT * FROM execution_wave_tasks WHERE task_id=?", (a,))
    assert wt["reservation_state"] == "RELEASED", \
        "the Task must be silently skipped (not crashed) while genuinely still reserved -- confirms the blast radius"


def test_restart_reconciliation_clears_stale_reservations_and_scheduling_works_again(git_repo, tmp_path):
    """The real fix, proven with a real restart: a fresh create_app()
    against the SAME DB (simulating the old process's own death) must
    clear the stale reservation via reconcile_on_startup() alone, and
    the exact same Task must be schedulable again afterward."""
    root, repo = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    client = build_client(settings)
    enable_parallel(repo, max_concurrent=2)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Restart reservation change", project_id=rid)
    a, _ = new_plan_task(client, cid, "A", "A", ["a.py"])

    db = client.app.state.db
    db.execute("INSERT INTO task_reservations(task_id,wave_id,state) VALUES(?,?,?)", (a, None, "RESERVED"))
    assert db.one("SELECT * FROM task_reservations WHERE task_id=?", (a,)), "sanity: the stale row is really there"

    # A genuine restart: a brand-new create_app() against the same DB
    # file -- execution_wave_service.reconcile_on_startup() runs
    # exactly as it would in production.
    restarted = build_client(settings)

    assert not restarted.app.state.db.one("SELECT * FROM task_reservations WHERE task_id=?", (a,)), \
        "stale reservation must be cleared by restart reconciliation"

    fake_launcher(restarted, "claude")
    run = restarted.app.state.execution_wave_service.run_execution_wave(cid)
    assert run["outcome"] == "LAUNCHED", run
    wt = restarted.app.state.db.one("SELECT * FROM execution_wave_tasks WHERE task_id=?", (a,))
    assert wt["reservation_state"] == "LAUNCHED", \
        "the exact same Task must be schedulable again after restart reconciliation, not permanently skipped"
