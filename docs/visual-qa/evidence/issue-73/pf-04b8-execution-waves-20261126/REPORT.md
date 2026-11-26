# PF-04b8 — Execution Waves deterministic provider fixture

## Identity, ownership, and source

- Exact PR #74 starting head: `fcf20fe750fdc13cad5174308c89916c929b375b`, tree `3316adb87418d7a05adba7fbd6cc18a7c14be40f`; local clean worktree commit `45ed1a25b6dd9bdbe420bc5bb00767e25196c2c5` had the identical tree. The `tests/test_execution_waves.py` blob was identical at base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3` and candidate (`633184fac09ecfa77b57f36b860374c215e1a4a2`).
- After the test-only patch, source file blob is `1dedf9a2c1daf5b3dfab8d6cb5a4677c7ac627bd`; staged source-only tree tested is `6986dc130abdc037d1b278af56cad9a33e80c7a1`. No production scheduler/provider code changed.
- One task-owned detached base worktree was created for comparison and removed after the tests. Base initially could not collect because it lacks `tests/__init__.py` and the installed `tests` package shadowed imports. An empty marker was added only inside that disposable base worktree (SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`), then removed with the worktree. Main, other task worktrees and shared runners were untouched.

## Reproduced cause and scoped change

On both base and candidate, the first two historical tests installed `fake_launcher(client, "claude")`. `Settings.agents` starts with Codex, and `_default_provider()` selects the first launchable entry, so the wave selects `codex`. `fake_launcher()` MERGES into the existing launcher map. The existing Codex launcher therefore remains configured to execute the real `codex` binary while the new fake only replaces/adds Claude. The restricted PATH has no Codex CLI. A corrected runtime diagnostic captured `selected_provider=codex`, `planned_provider=codex`, launcher executable `codex`, actual launch result `EXECUTION_FAILED: Codex CLI chưa được cài hoặc không có trong PATH`, `wave_outcome=NO_TASKS_LAUNCHED`, and zero sessions. This establishes a test-fixture provider mismatch, not a scheduler reservation/concurrency failure.

Only the two fixture calls in `tests/test_execution_waves.py` changed to use `codex`. They now assert the scheduler's default is Codex before installing that matching disposable launcher; this overwrites the selected provider entry with the real bash/PTY fixture while leaving other provider entries untouched. The reservation race, sibling failure behavior, separate PTY/PID assertions, worktree isolation, review, Git commits, serialized integration and updated-base assertions remain intact. `test_real_two_task_wave_parallel_safe_full_closed_loop` and `test_execution_wave_2_uses_updated_base` already used Codex and passed on the pre-patch candidate; their imported `set_fake` now uses the previously repaired test seam. No reviewer/gate logic was mocked.

## Verification

Environment: Python 3.14.4, pytest 9.2.0, `PATH=/usr/bin:/bin`.

- Candidate before patch, four historical nodes: **2 failed / 2 passed**, exit 1. Red IDs were double-tick and sibling-failure; actual assertions were zero sessions and `NO_TASKS_LAUNCHED`.
- Base comparison after the temporary import marker: the same two nodes **2 failed**, exit 1. This shows the fixture defect predates the candidate. The first base attempt before adding the marker was a collection/import error (exit 4), not a test result.
- Post-patch entire `tests/test_execution_waves.py`: **22 passed / 1 skipped**, 0 failed/errors, exit 0. The one unchanged real-Claude launch test is skipped because `claude` CLI is not installed; it was not replaced, waived or counted as pass.
- Four historical nodes map to PASS in the final module JUnit: see `historical-node-resolution.tsv`.
- Related autonomous readiness/duplicate-launch/failure, Review Fix Loop launch, and visual contract/execution regression selection: **21 passed**, exit 0.
- Exact commands, node selectors, run timestamps, exit codes, stdout/stderr, collection/JUnit counts and source identity are in `replay-commands.txt` and the logs/JUnit. `git diff --check` was run before publication.

The corrected diagnostic is a one-off task-owned test that exercised the real wave planner, launcher manager, and SQLite result row; its temporary source file was removed. An earlier diagnostic assertion incorrectly expected the launcher map to be replaced rather than merged; that probe exited 1 on its instrumentation assertion while printing the actual launch result. The corrected diagnostic then passed with the assertions matching the observed merged map. Both logs are retained. Other evidence includes candidate red, base collection failure/red reproduction, intermediate green, final complete module green, related regressions, JUnit and node mapping.

## Gate and rollback

This is one deterministic module, not a full suite result. Gate A remains FAIL pending the full suite; Gate E is not PASS without required CI on the final exact SHA. B/C are unchanged; this test-only repair does not requalify D/F. Keep #74 draft and #73 open. Rollback by reverting only the PF-04b8 task commit; this patch changes tests only. The temporary base worktree/marker were removed.
