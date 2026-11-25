# PF-03c — Required test-stage membership

Date: 2026-11-25 (Asia/Bangkok). Scope: `TaskDecisionService` stage membership only. No full suite, provider, NovaRetail, CI activation, merge, or deployment.

## Reproduced issue

At start, PR #74 was draft at exact head `dd0c96e40b463e06a7738a29522312e0756449ba`, base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. The current source computed a required stage count from `load_contract()`, then compared it with the count of distinct latest result stages. Consequently, rows for an unrelated stage could satisfy the count while a contract-required stage was absent. Builder status likewise accepted all observed PASS rows without requiring each configured stage. A present malformed non-visual contract was also collapsed into the legacy zero-required path by the integration gate.

The regression creates real temporary Git repositories, loads their checked-in `PROJECT.yaml` through `load_contract`, stores results in a real SQLite `Database`, and evaluates the real `TaskDecisionService` against exact current-commit rows.

Before repair: `tests/test_task_decision_required_stages.py` — **10 failed / 3 passed / 0 errors / 0 skipped, exit 1**. The red cases cover missing named stages hidden by unrelated PASS, stale-SHA rows, latest QUEUED/RUNNING/SKIPPED results, latest-run selection, required visual stage and waiver handling, backend opt-out, and malformed-vs-legacy contracts.

## Change

- `TaskDecisionService._required_test_stage_names()` obtains required names from the same `load_contract()` used by `TestRunner`.
- Integration and builder decisions now select the latest result for each required stage by name. Missing required rows and latest QUEUED/RUNNING/SKIPPED rows are `NOT_RUN`; required FAIL/TIMEOUT remains blocking. Unrelated extra rows cannot satisfy or replace a named stage.
- Integration baseline waivers retain their exact `(gate, test_identifier, failure_fingerprint)` behavior for ordinary stages. `visual_qa` FAIL/TIMEOUT cannot be waived into a pass. This does not inspect or verify visual artifact contents; that is separate PF-03d scope.
- A truly absent `PROJECT.yaml` and a valid zero-required-stage contract retain the historical integration PASS behavior. Builder no-contract legacy results retain their prior all-observed-rows behavior. A present malformed contract fails closed (integration `UNKNOWN`, builder `FAIL`). Task opt-in still requires `visual_qa`; an unmarked backend task does not.

## Verification

Using existing Python 3.14.4 / pytest 9.2.0 venv, artifacts outside the checkout during execution:

- Red: `python -m pytest -vv tests/test_task_decision_required_stages.py --junitxml=<owned-artifact>/before-red.xml` — **10 failed / 3 passed**, exit 1, JUnit start 2026-11-25 19:43:46 ICT.
- Green: `python -m pytest -q tests/test_task_decision_required_stages.py --junitxml=<owned-artifact>/after-green.xml` — **13 passed**, exit 0, JUnit start 19:45:38 ICT, 2.528s.
- Combined targeted: `python -m pytest -q tests/test_task_decision_required_stages.py tests/test_test_runner_source_identity.py tests/test_baseline_waivers.py tests/test_project_test_timeout.py tests/test_cross_repo_integration.py tests/test_real_merge.py tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_workflow_decision_ux.py tests/test_b06_sandbox.py::test_testrunner_stays_direct_host_under_auth_mode_none tests/test_b06_sandbox.py::test_testrunner_uses_sandbox_under_auth_mode_required --junitxml=<owned-artifact>/targeted.xml` — **77 passed / 0 failed / 0 errors / 0 skipped**, exit 0, JUnit start 19:46:01 ICT, 67.165s.
- `./scripts/preflight.sh` — `preflight PASS`, exit 0 at 19:47:18 ICT. After tests, `docker ps --filter name=wm-exec-` found zero matching containers.
- Existing Starlette/httpx and `asyncio.iscoroutinefunction` deprecation warnings remain. No full suite was run.

The worktree began at local commit `22884d9ebcb7939b48298f9b1755c6909d1bbc9a`, whose tree matched the then-current PR head. Tested code commit `d9b0cf203d2f5ab4a457cd290673ac415e250259` has tree `07dfaa2662c09373f60e90b4bb9a403b578f67ec`; published source commit `aaf03776f115a191c65de59384b1e12673713567` is a normal expected-head fast-forward from `dd0c96e40b463e06a7738a29522312e0756449ba` with that identical tree. Test evidence is tree-equivalent to the published source; the subsequent commit adds only this report, raw logs/JUnit and checksums.

## Gates and limits

A remains FAIL: this is targeted verification, not a full-suite run. B/C remain blocked; D is prior local evidence only; E is not PASS without required exact-head CI; F preserved. PR stays draft. Stage-name membership does not prove that a visual command emitted a valid artifact or that an artifact is bound to the current source; do not infer PF-03d from this patch.

Rollback: revert only the PF-03c source commit on the task branch. No main/WIP, production, shared daemon, CI setting, or other lane was modified.
