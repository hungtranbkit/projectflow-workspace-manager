# PF-04b6 — fix-task launch callback fixture

## Identity and scope

Existing task worktree: branch `agent/codex/issue-73-engineering-fixture-20261125`, clean at task start, local commit `bf75785c4c8417066d65853402b23cb5f9964005` / tree `c2bafb79eca763f08f2629ce2a56d97d498893ae`; PR #74 exact remote start head `59b255557e399cd0b59a12ca1e0018a711224884` (same tree). Base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. Source blob before `edf0fdaeeffb77f2ccb6f0f1a2cdf547c3433099`; after `e25d5058e463adad8fab1c2ead527dafc6e3a75d`. Only `tests/test_review_fix_loop.py` plus evidence/context changed; no production source or provider calls.

Runtime: Python 3.14.4, pytest 9.2.0, `PATH=/usr/bin:/bin`. Initial red on candidate and base reproduced both residual tests returning `EXECUTION_FAILED` / missing `fix_task_id` because Codex CLI was not available. Base needed an empty `tests/__init__.py` in the disposable base worktree to avoid installed-package shadowing; it was scratch-only and removed after reproduction.

## Test-only change

The two orchestration tests now replace only the already-injected `review_fix_orchestrator._start_builder_session` callback with a deterministic test callback. It records the passed workspace row and returns `test-session-<workspace id>` without spawning a process or inserting an agent session. Assertions require `FIX_BUILDER_LAUNCHED`, returned Fix Task/session IDs, correct transferred workspace/task/branch/path, real fix commit, and finding-chain resolution. A separate negative callback raises a synthetic test exception and asserts `EXECUTION_FAILED`, no claimed session/fix IDs or success event, one recoverable Fix Task retaining the same worktree, and `AUTO_EXECUTION_FAILED` recorded. Production callback behavior is not replaced globally.

## Verification

- Red before patch: two target orchestration tests on exact candidate start tree, 2 failed / exit 1. The actual response payload shows `EXECUTION_FAILED`, governing task id 2, missing-Codex-CLI message, and no `fix_task_id`; see `candidate-red.log/xml`. No base result is claimed.
- Focused changed nodes after patch: **3 passed**, exit 0 (`targeted.log/xml`).
- Full `tests/test_review_fix_loop.py`: **31 passed**, 0 fail/error/skip, exit 0; collect-only 31. All **17/17 historical failed node IDs** for the module map to PASS (`historical-node-resolution.tsv`). The real-provider suite is a separate module and was not run.
- Related regressions: `tests/test_release_pipeline.py`, `tests/test_visual_qa_contract.py`, `tests/test_visual_qa_execution.py`: **46 passed**, exit 0.
- Builder orchestration regressions: `tests/test_autonomous_execution.py::test_execution_failure_recorded`, `::test_repeated_failure_resets_after_success`, and `tests/test_prompt_delivery.py::test_delivery_failure_then_manual_retry_succeeds`: **3 passed**, exit 0. The prompt-delivery test's shell child is an existing disposable test fixture, not a product provider session.
- `git diff --check`: exit 0. No full suite, real provider, CI, CI setting, merge or deployment.

Times ICT on 2026-11-26 are recorded by file timestamps in the accompanying run logs; logs contain exact pytest commands, stdout/stderr, summaries, and exit codes. Candidate red run exited 1 (2 failed); post-patch focused run exited 0 (3 passed); complete module exited 0 (31 passed); release/visual regressions exited 0 (46 passed); builder orchestration regressions exited 0 (3 passed). `SHA256SUMS.txt` covers evidence files except itself. Host and pytest temporary paths are sanitized.

## Rollback and gates

Rollback only by reverting this PF-04b6 commit on PR #74. No main/WIP, production, shared runner, Nova, or credentials touched. Full-suite Gate A and exact-head CI Gate E remain not PASS; no change to B/C or other gate status is claimed.
