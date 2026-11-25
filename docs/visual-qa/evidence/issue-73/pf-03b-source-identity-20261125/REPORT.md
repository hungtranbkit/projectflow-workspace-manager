# PF-03b TestRunner source identity — 2026-11-25

## Outcome

Closed the narrow source-identity gap where `TestRunner.start()` sampled HEAD separately for each stage and `_run()` trusted only the process exit code. A changing or dirty worktree could therefore produce a PASS row pinned to a commit whose source was not what the command read.

The repair keeps the existing TestRunner and TaskDecisionService as the only run/gate path. It does not add a stage authority, evidence manifest, provider call, or full-suite run.

## Behavior now

- `GitWorkspaceService.source_state()` reads a stable HEAD, hashes `git diff HEAD --binary` for tracked/index changes, and records untracked paths. It reads HEAD and tracked diff twice and fails if they change during inspection.
- `TestRunner.start()` snapshots source identity before and after stage-contract loading. If those observations differ, it fails closed before queueing commands.
- All stage rows receive one commit SHA captured at queue time, and only when the source tree is clean. A dirty tree remains runnable for developer feedback, but rows use `tested_commit=NULL` and carry `SOURCE_NOT_COMMIT_BOUND`; they cannot be counted as current commit evidence.
- Before each stage and after its command, the runner rechecks HEAD and source content. Detected drift makes the stage FAIL even if the shell command exits 0. Existing TIMEOUT/FAIL/SKIPPED handling and `TEST_PASS`/`TEST_FAIL` events remain intact.
- `TaskDecisionService` returns `NOT_RUN` for builder/integration evidence when the current source is dirty or no longer matches the observed HEAD. An integration contract with zero required stages keeps its existing legacy behavior.
- Git-ignored generated artifacts remain outside source identity. New unignored paths appearing during a clean run invalidate that run's SHA binding. Dirty worktrees can still run, but are explicitly unbound.

## Before/after proof

The red run used real temporary Git repositories and the actual GitWorkspaceService, Database, TaskDecisionService, and a synchronized shell runner that invoked the configured command with `shell=True`:

- Before fix: `tests/test_test_runner_source_identity.py` — **4 failed / 1 passed, exit 1**. It reproduced a branch moving A→B before command execution, a commit during the command that caused the real shell to read B while the row named A, dirty tracked source incorrectly inheriting A, and dirty tracked-source mutation during execution still being recorded PASS. The pre-existing “pass A then commit B” staleness test passed.
- After fix: source identity module — **9 passed / 0 failed, exit 0**.
- Combined regression run — **60 passed / 0 failed / 0 errors / 0 skipped, exit 0**.

The combined run included source-identity cases; bounded timeout; cross-repository and real merge tests; visual contract fail-closed, visual stage failure and backend opt-out; TaskDecisionService workflow decisions; host runner mode; and the mandatory Docker sandbox runner test. The sandbox test used its existing `wm-exec-* --rm` container with network disabled; post-run `docker ps --filter name=wm-exec-` returned no containers.

## Replay

The exact local Python executable was `/home/dell/workspace/.worktrees/projectflow-visual-qa-73/.venv/bin/python` (Python 3.14.4, pytest 9.2.0); it was used read-only. From the ProjectFlow worktree root:

```sh
export VENV="/path/to/existing-projectflow-test-venv"
"$VENV/bin/python" -m pytest -q tests/test_test_runner_source_identity.py \
  --junitxml=docs/visual-qa/evidence/issue-73/pf-03b-source-identity-20261125/after-green.xml

"$VENV/bin/python" -m pytest -q \
  tests/test_test_runner_source_identity.py \
  tests/test_project_test_timeout.py \
  tests/test_cross_repo_integration.py \
  tests/test_real_merge.py \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py \
  tests/test_workflow_decision_ux.py \
  tests/test_b06_sandbox.py::test_testrunner_stays_direct_host_under_auth_mode_none \
  tests/test_b06_sandbox.py::test_testrunner_uses_sandbox_under_auth_mode_required \
  --junitxml=docs/visual-qa/evidence/issue-73/pf-03b-source-identity-20261125/targeted.xml
```

The logged invocation used that existing venv read-only. JUnit timestamps: source module 18:39:29 ICT (9 cases, 2.257s); combined run 18:39:43 ICT (60 cases, 51.292s); sandbox run 18:34:39 ICT (1 case, 1.618s). The combined run ended at approximately 18:40:35 ICT; its captured shell exit was 0. Standalone sandbox check also passed 1/1 and left no task-owned container. Existing Starlette/httpx and `asyncio.iscoroutinefunction` deprecation warnings remain.

## Limits

This is a before/after observation, not an immutable filesystem snapshot or exclusive worktree lease. A transient change and restoration (A→B→A) entirely between observations can evade the check; concurrent writers are not locked. Untracked artifacts present before queue make the run unbound, while new unignored paths during a clean run fail the binding. An atomic snapshot/lease plus persistent content digest would require separate architecture/schema scope.

## Identity and gates

- Starting clean task branch: `agent/codex/issue-73-engineering-fixture-20261125`, parent `f1d09c48de10a2c41d8dc907c9d1799df467832f`.
- PR #74 expected head at PF-03b start: `e95e5d77b189d3774d02b37a9efd1475b4e88d2d`; base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. Tested local source commit `168abf133bdd73d57d0bc74c650b046bc3b072a5` has tree `57ba876fc124e247ce730da51d09c31b71637bb8`; published source commit `1ac2e825eebc932c5eb4cf3d67b1f8e751c27316` has the same tree and is a normal expected-head fast-forward. The tree identity, rather than local commit SHA identity, binds the recorded test run to the published source content.
- No full suite, provider, CI, NovaRetail, production, or deploy operation was performed. Gate A remains FAIL; B/C remain blocked; D prior local evidence only; E not PASS; F preserved.
- Rollback: revert only the PF-03b commit(s) on PR #74. No worktree, main, or WIP cleanup was performed.
