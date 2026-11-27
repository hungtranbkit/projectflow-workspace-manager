# PF-04b2 — Test Design deterministic runner fixture

Date: 2026-11-26, ICT (UTC timestamps in logs use the actual host clock). Scope is **test-only**: `tests/test_test_design.py`; no production changes, real provider invocation, or full-suite run.

## Ownership and source identity

- PR #74 branch: `agent/codex/visual-qa-73`. Exact remote head at start: `624b88ca9fd86db67c1d1f309c41468bacc22f82`, tree `a433f91871680da8d2fd34e5daaa497d2702a3af`.
- Existing local task worktree/branch: `$WORKTREE`, `agent/codex/issue-73-engineering-fixture-20261125`; clean at start, same tree as remote PR. Only this ProjectFlow task worktree was active. Main was not checked out or changed.
- Base for the red comparison: current remote `main` commit `cbeaffe417765f2778eeb9ad96cd0db21a9acb69`, tree `c9590bb7ad1181b5d5d306b9e7d4efab4c098f76`. Candidate Test Design file blob at the base and PR head was identical: `23522d1a4af7d1b14923191c1b0f03615cf603f0`.
- Candidate file blob after the test-only change: `7ac58b7fd3bb4534de4a38d22e281936bdfa8fdf`. Only source file changed is `tests/test_test_design.py`.
- Python 3.14.4, pytest 9.2.0 from the existing task venv; 115 GB disk free at preflight. No packages installed.

## Red reproduction and cause

Commands on both exact base and tree-identical PR candidate used the same interpreter, restricted `PATH=/usr/bin:/bin`, three representative node IDs, and JUnit. Candidate JUnit start was 2026-11-26 02:38:52.737 ICT; base JUnit start was 02:39:13.008 ICT. Both exited 1 with 3 failures / 0 errors / 0 skips:

- `test_test_design_structured_result`: actual outcome `EXECUTION_FAILED`, expected `READY`.
- `test_review_pass_approves_test_case_set_and_plan`: review returned 404 “No Test Design exists for this Change yet”.
- `test_coverage_full`: test cases were not created, leaving `REQ-001` and `REQ-002` uncovered.

The failure is the same seam as PF-04b1: `PlannerAgentInvoker.invoke()` resolves `which("claude")` before it invokes its injected runner. Test Design's `set_fake()` and two custom runners replaced only `.runner`; with the real CLI absent from restricted PATH, the fake payload was never used. `do_design()` checked only HTTP 200 and therefore deferred the failed stage outcome to later assertions. The same red behavior on base and PR candidate shows this is a pre-existing deterministic fixture defect, not a candidate regression. It does not establish that every old Test Design fingerprint has this cause.

Raw (host-path-sanitized) logs and JUnit are `base-red.*` and `candidate-red.*`.

## Test-only repair

`set_fake_runner()` now injects both the deterministic executable resolver and runner, and asserts `planner_service`, `test_design_service`, and `test_review_service` share the same invoker instance. `set_fake()` and both custom runner cases use it. `do_design()` checks `outcome == READY`; `do_review()` checks `outcome == REVIEWED`, retaining `REJECT` and refinement verdict semantics. Added a regression where CLI resolution returns unavailable and verifies `EXECUTION_FAILED`, no test plan, and zero runner calls. No fake executable was placed on PATH. The real-provider lifecycle test remains unchanged and was not run.

## Verification

Environment: Python 3.14.4 / pytest 9.2.0, existing task venv. No provider subprocess was invoked.

1. Candidate module, 2026-11-26 02:40:20–02:41:17 ICT:

```sh
PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  "$TEST_VENV/bin/python" -m pytest -q -ra tests/test_test_design.py \
  -k 'not test_real_test_design_lifecycle_end_to_end' --junitxml=test-design.xml
```

Exit 0. Collection inventory is 45; 44 passed, the one named real-provider test was deselected (not skipped), 0 failures/errors among selected tests. JUnit: 44 tests, 0 failures/errors/skips; 51.917 s.

2. PF-04b1 lifecycle + planner envelope + visual contract/execution regression, 2026-11-26 02:41:49–02:42:51 ICT:

```sh
"$TEST_VENV/bin/python" -m pytest -q -ra \
  tests/test_spec_lifecycle.py -k 'not test_real_spec_lifecycle_end_to_end' \
  tests/test_planner_service.py::test_malformed_envelope_produces_execution_failed_no_plan_row \
  tests/test_planner_service.py::test_planner_reports_allowlisted_provider_usage_limit_without_raw_payload \
  tests/test_planner_service.py::test_malformed_inner_result_produces_output_invalid_no_plan_row \
  tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py \
  --junitxml=regression.xml
```

Exit 0. JUnit: 58 tests, 0 failures/errors/skips; 57.458 s. The three planner envelope cases inject local fake runners; no provider subprocess.

3. Collection-only inventory: `PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 "$TEST_VENV/bin/python" -m pytest --collect-only -q tests/test_test_design.py`; exit 0, 45 nodes.
4. `git diff --check -- tests/test_test_design.py` exited 0.

No full suite, provider, build, CI activation, merge, or deployment was performed. These targeted results do not change Gate A or overall A–F acceptance.

## Files and replay evidence

- `candidate-red.log/.xml`, `base-red.log/.xml`: before-fix red cases.
- `test-design.log/.xml`: deterministic module green.
- `regression.log/.xml`: PF-04b1 and visual/planner regressions green.
- `collection.log`: 45-node inventory.
- `SHA256SUMS.txt`: literal SHA-256 hashes for evidence files; manifest intentionally excludes itself.

Rollback is a revert of the PF-04b2 source commit on the PR branch; no reset of WIP is needed. Continue only after the next task checks ownership and scope.
