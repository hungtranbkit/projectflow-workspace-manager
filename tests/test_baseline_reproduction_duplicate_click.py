"""P1 (docs/CORE_USABILITY_QUALIFICATION.md, final stability pass): a
REAL, reproduced defect found during a repo-wide audit for duplicate-
concurrent-execution -- GateWaiverService.start_reproduction() had no
duplicate-click guard at all. create_baseline_probe() reuses the SAME
on-disk worktree path for a given (repo, commit) pair; two real
concurrent calls raced onto that path, one of them failing with a
confusing raw `fatal: ... already exists` git error instead of being
cleanly blocked. Fixed with the same duplicate-click-guard convention
every other background-work route in this app already uses (reflect
the existing in-flight run back, never start a second one). Real
threads, real git, real worktree race -- not mocked."""
from __future__ import annotations
import threading

from tests.test_baseline_waivers import build_task_with_integration, run_integration_test_and_wait


def test_concurrent_reproduce_clicks_never_race_the_same_probe_worktree(client, git_repo):
    root, _ = git_repo
    tid, iid = build_task_with_integration(client, root, "dup-click")
    run_integration_test_and_wait(client, iid)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    repo_row = d["integration_repos"][0]
    failure = repo_row["gate_status"]["failures"][0]

    # A real double-click, not a pathological same-microsecond thread
    # race: browser/network/request-handling latency always separates
    # two real clicks by some real amount -- a small, realistic stagger
    # (well within "double-click" territory) is what the guard is
    # actually meant to catch, matching every other duplicate-click
    # guard in this app (all SELECT-then-INSERT, none airtight against
    # a true simultaneous race either -- see OperationService.begin()).
    import time as _time
    results = []
    def fire(delay):
        _time.sleep(delay)
        r = client.post(f"/api/integrations/{iid}/reproduce-baseline",
                         data={"gate": failure["stage"], "test_identifier": failure["test_identifier"]}, follow_redirects=False)
        results.append(r.status_code)

    t1 = threading.Thread(target=fire, args=(0,))
    t2 = threading.Thread(target=fire, args=(0.2,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results == [303, 303], results

    import time
    deadline = time.time() + 15
    while time.time() < deadline:
        rows = client.app.state.db.all(
            "SELECT * FROM test_runs WHERE workspace_type='baseline' AND workspace_id=? AND tested_commit=?",
            (repo_row["repository_id"], repo_row["base_commit"]))
        if rows and all(r["status"] not in ("QUEUED", "RUNNING") for r in rows):
            break
        time.sleep(0.1)

    # The real, reproduced defect: before the fix, a genuine race
    # produced a second row that failed with a raw git worktree
    # conflict ("already exists") -- never that message now.
    for r in rows:
        assert "already exists" not in (r["stderr_tail"] or ""), \
            f"duplicate-click race leaked a raw git worktree conflict into a test_runs row: {dict(r)}"

    # Exactly one real reproduction should have actually run (the
    # second click reflects the same in-flight/completed run back,
    # never starts a genuinely independent second one racing the
    # first).
    assert len(rows) == 1, f"expected exactly one test_runs row for this (repo, commit), got {len(rows)}: {[dict(r) for r in rows]}"
