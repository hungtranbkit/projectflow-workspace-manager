# PF-04e — reservation recovery fixture

## Source and safety

- PR #74 source before: `8458536bd851afd6691da28d3101806eff1baeee`, tree `e6792aa01a1691f040fff9367797bf88812621ea`; base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`.
- Test-only source commit: `b19b959f01906adebc390d0024aecf44fe932ecf`, tree `2f83e0c54314b3ab84ec8e99401391720b61f7ff`.
- Only `tests/test_execution_wave_reservation_stuck_after_restart.py` changed. No application code changed.
- Tests used existing Python 3.14.4 / pytest 9.2.0 venv, restricted `PATH=/usr/bin:/bin`, unset `DOCKER_CONTEXT`, and `DOCKER_HOST=unix:///tmp/pf-04e-no-docker.sock`. No provider CLI or shared Docker daemon was used.

## Root cause and red/green

The scheduler's `_default_provider()` selects `codex`. The module's `fake_launcher()` helper merges a launcher under the supplied provider key; both tests supplied `claude`, leaving the selected `codex` launcher absent. Candidate red telemetry recorded `execution_wave_tasks.provider=codex`, reservation `FAILED`, zero `agent_sessions`, and the missing Codex CLI message. Base and candidate both cleared the stale reservation on restart; scheduling then failed only at launcher resolution.

The test fixture now asserts the selected provider is `codex` and installs the existing real-PTY `bash -c 'echo READY; cat'` test launcher under that provider. It does not fake the scheduler, provider result, DB reservation, or Git/worktree behavior.

- Base reproduction: `base-reservation.xml` / `.log`, exit 1, 1 passed + 1 failed.
- Candidate red: `reservation.xml` / `.log`, exact pre-fix SHA above, exit 1, 1 passed + 1 failed.
- Green on committed source SHA: `exact-reservation.xml` / `.log`, exit 0, 2 passed.
- Related focused regressions: `exact-related.xml` / `.log`, exit 0, 19 passed across duplicate tick, sibling isolation, launch failure, TaskDecisionService low-risk completion, and required-stage gates.

Exact execution window and source identity are in `exact-meta.txt`. Logs have task-local absolute paths redacted and trailing whitespace normalized; JUnit test identities/counts and outcomes are unchanged.

## PF-04d evidence correction

The PF-04d productization command window in its original summary was wrong. It is corrected from preserved command metadata and original JUnit timestamps in `../pf-04d-productization-audit-20261127/`. This is an evidence-only correction; no PF-04d test was rerun and prior pass counts are unchanged.

## Gates and rollback

This is one deterministic test-fixture correction, not a full-suite run. Gate A remains unqualified; E is not PASS without exact-SHA required checks. B/C and prior safety/approval blockers are unchanged. Roll back only commit `b19b959` with `git revert b19b959f01906adebc390d0024aecf44fe932ecf` on the task branch.
