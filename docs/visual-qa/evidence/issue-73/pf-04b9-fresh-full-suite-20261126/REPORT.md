# PF-04b9 follow-up — fresh full-suite qualification checkpoint

Observation date: 2026-11-26 (Asia/Bangkok, UTC+07:00).

## Exact source and runner decision

- PR #74 was rechecked before execution: remote branch and PR head `70d467ce7532d6f4fdcb9a238157daba3737afa1`, tree `3639037adab78f941f22817b235cb7f43373e579`.
- The existing task worktree was clean and detached at that exact remote SHA for the checks below. No task source was modified by this checkpoint.
- Dell has enough raw compute/disk for the suite (12 CPUs, ~17 GiB memory available, ~115 GiB disk free), Docker 29.7.2, 29 existing networks, `DefaultAddressPools=null`; no Docker configuration or networks were changed. The test-local network pool code is present in source. Playwright image(s) are already cached.
- Dell's pinned UIFix verification passed: UIFix 1.2.6, Node v26.7.0, Node Playwright 1.60.0. Python Playwright is absent from the task-selected venv; a fresh `pytest --collect-only -q -ra -rs` reports the normal module-level skip at `tests/test_ui_high_frequency_screens.py:24` (`No module named 'playwright'`). It was not installed or forced past `importorskip`.
- Claude auth status is logged in under the existing Claude.ai identity; Codex CLI is not logged in. No provider invocation was made in this checkpoint.

## Actual work completed

- Exact-source `./scripts/preflight.sh`: exit 0 (`preflight PASS`).
- Exact-source `./scripts/build.sh`: exit 0 (`build PASS`).
- Exact-source `./scripts/smoke.sh`: exit 0; one smoke test passed.
- Pinned UIFix verification: exit 0.
- Fresh deterministic prepass under `PATH=/usr/bin:/bin`: 6 passed, 0 failures/errors/skips, exit 0. Nodes covered the productization planner-concurrency fixture, actual-scope-overlap, both baseline-waiver flows, and both stuck-test-run recovery cases. JUnit/log/meta are included.
- Fresh collect-only on exact source: module totals sum to 1,627 test items; the high-frequency browser module is a separate collection skip because Python Playwright is absent. This is collection inventory only, **not** test execution and not a suite pass.

## Why full `scripts/test.sh` was not started

Two observed runner conditions make the authenticated provider suite unsafe to launch on either available host without modifying other work or shared identity state:

1. On Dell, an unrelated pytest process was active in `/home/dell/workspace/projectflow-workspace-manager` (the main checkout), and an existing Claude session was active. The real-provider tests in this suite write `~/.claude.json` and their cleanup removes every `/tmp/pytest-of-dell/` project entry, not only the entry created by that test. One such temporary trust entry already existed at observation time. Running the suite could therefore delete trust state owned by another active session. This was not modified.
2. The authorized HP alias was checked read-only. HP had only 13 GiB free on a 95%-full root filesystem, swap fully consumed (11 MiB free), 11 provider/agent processes, and no Claude CLI/auth available. It is not a safe full-suite runner for this workload.

The full suite was therefore **not started** via shell or TestRunner. I did not hide the installed CLI from PATH, fake provider success, install dependencies, copy credentials, change HOME/config, interrupt another process, or mutate Docker/production. The current proof is a precise shared-state/runner blocker, not an A-gate result. Use a maintainer-provided isolated runner/config where provider tests can authenticate without touching another session's `~/.claude.json`; alternatively repair those tests to use an isolated test-owned Claude config path, then requalify on exact source.

## Gate/checkpoint

- A: not PASS. Preflight/build/smoke and 6 targeted nodes passed, but the complete suite was not executed.
- B/C: unchanged; visual approval/equivalent Nova reference remain blocked.
- D: historical state only; not requalified here.
- E: not PASS. PR #74 exact SHA had no verified required checks in this checkpoint; do not infer CI from local tests.
- F: preserved. No production, dirty main, unrelated WIP, Docker daemon/network, provider config, CI configuration, merge, or deployment changes.

The complete run should be restarted only when one isolated runner is available with sufficient disk/resources, exact source SHA, pinned browser/runtime and an authenticated provider mechanism that does not sweep unrelated `/tmp` trust entries. It must run the unchanged `./scripts/test.sh` selection with `-rs` and JUnit; collect-only is not a substitute.
