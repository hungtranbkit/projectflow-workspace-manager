# PF-04b5 — Review Fix Loop deterministic fake seam

## Scope and source identity

Only test fixture code in `tests/test_review_fix_loop.py` changed. Production code, provider behavior, security/review gates and real-provider tests are unchanged. PR #74 expected head at start: `4df44ae5a8695ebb836b6b0d5c1401fd4ef904b7`, tree `4e1efedf7f27b8a8de77e2eacf73c65de05510dc`; base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. The existing local task branch was clean and tree-equivalent at start. Restricted environment: Python 3.14.4, pytest 9.2.0, `PATH=/usr/bin:/bin`.

Source file blob before change: `53256ea9a209a892a1d7f7b2872da0de7bcfbc28`; patched source blob: `edf0fdaeeffb77f2ccb6f0f1a2cdf547c3433099`. Local pre-change commit/tree: `eb7e9064a8feaa153d0fe7675da99b19d428ab6d` / `4e1efedf7f27b8a8de77e2eacf73c65de05510dc`.

## Red proof and diagnosis

Candidate’s three requested tests failed, exit 1, with `claude CLI not found in PATH`: valid code review, pass-with-findings, and dedicated security work product. Base reproduced those same nodes after adding an empty, task-scratch-only `tests/__init__.py` to resolve installed `tests` package shadowing (base predates the tracked package marker); all three failed. The source calls `PlannerAgentInvoker.which('claude')` before `.runner`. Code, security and planner services share the same invoker in `app/main.py`.

## Repair and verification

A test-local `set_fake_runner()` now asserts code-review, security-review and planner services share the same invoker, then injects both a test-only `which('claude')` resolver and fake runner. All deterministic fake assignments, including custom diff/security runners, use it. Added unavailable-CLI coverage verifies the resolver returns no executable, runner is never invoked, response has no verdict, and neither `review_runs` nor a `REVIEW_REPORT` is persisted. The real-provider test is in the separate `tests/test_review_fix_loop_real.py` module and was not invoked.

- Candidate red command: `PATH=/usr/bin:/bin $VENV/bin/python -m pytest -q -ra <three requested node IDs> --junitxml=<candidate-red.xml>` — 3 failed, exit 1. Base used same 3 IDs after the temporary package marker — 3 failed, exit 1.
- Full affected module: `PATH=/usr/bin:/bin $VENV/bin/python -m pytest -q -ra tests/test_review_fix_loop.py --junitxml=<review-fix-loop.xml>` — **28 passed, 2 failed, 0 errors/skips**, exit 1; collect-only found 30 tests.
- Historical mapping: 17 failed node IDs for this module: 15 now PASS, 2 still FAIL; see `historical-node-resolution.tsv`. The two residual fix-task tests fail after review succeeds because the follow-up Codex builder launch returns `EXECUTION_FAILED`, `Codex CLI chưa được cài hoặc không có trong PATH`; `fix_task_id` is therefore absent. This is an independent Builder runtime prerequisite, not folded into this test-only reviewer seam.
- Focused related regressions: release pipeline + visual contract/execution **46 passed**, exit 0. The two residual fix-task failures were separately rerun with `--showlocals`, exit 1, and their exact outcome is preserved in `fix-task-failures.log/xml`.
- `git diff --check` exit 0. No provider call or full suite.

Times (ICT, 2026-11-26): candidate red 05:52:55; base red 05:53:50; module completion 05:55:33; collection 05:56:29; scoped regressions 05:57:53. Raw sanitized command output and JUnit are in this directory; `SHA256SUMS.txt` covers every evidence file except itself.

## Remaining status / rollback

This milestone does not qualify full-suite Gate A; two module tests still need an authorized Codex Builder CLI environment or an explicitly approved deterministic builder seam in a separate scope. No full suite, provider test, CI run/enablement, merge, deployment, Nova, production, main/WIP, or shared-runner mutation. Rollback only by reverting the PF-04b5 commit on the task PR branch; the base scratch worktree was task-owned and removed after evidence capture.
