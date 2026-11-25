# PF-03a integration contract source — 2026-11-25

## Result

Confirmed and fixed the source-selection defect in `IntegrationService.verify_integrated_state()`. The service created a detached Git probe at the integrated commit but read `PROJECT.yaml` from the canonical checkout. It now loads the required-stage contract from that detached probe. Commands continue to execute in the probe, and required Visual QA still fails closed when the candidate contract is missing or invalid.

This is a targeted local milestone, not full-suite, CI, or issue #73 completion.

## Reproduction and correction

The regression tests use real temporary Git repositories and `GitWorkspaceService.create_baseline_probe()`. The canonical `main` ref and candidate commit contain different `PROJECT.yaml` files; the test runner executes the selected shell command from the actual detached worktree and records its working directory and timeout.

Before the fix, `tests/test_integration_contract_probe.py` reported **4 failed / exit 1**:

- The candidate's test command was replaced by the canonical command.
- A candidate with no `PROJECT.yaml` was allowed to use the valid canonical visual contract.
- A candidate with an invalid visual contract was allowed to use the valid canonical visual contract.
- A dirty canonical `PROJECT.yaml` overrode the committed candidate contract.

After the fix, these four tests passed. They also cover the candidate's configured 19-second timeout and a valid non-opt-in legacy contract. The tests assert that failed visual contract validation starts no stage command.

## Verification

Environment: Python 3.14.4, pytest 9.2.0. Runner executable was the existing task environment at `/home/dell/workspace/.worktrees/projectflow-visual-qa-73/.venv/bin/python`; no environment or dependency files were changed.

Commands, from repository root:

```sh
/home/dell/workspace/.worktrees/projectflow-visual-qa-73/.venv/bin/python -m pytest -q \
  tests/test_integration_contract_probe.py \
  --junitxml=docs/visual-qa/evidence/issue-73/pf-03a-contract-source-20261125/before-red.xml
# Before source fix: 4 failed, exit 1

/home/dell/workspace/.worktrees/projectflow-visual-qa-73/.venv/bin/python -m pytest -q \
  tests/test_integration_contract_probe.py \
  --junitxml=docs/visual-qa/evidence/issue-73/pf-03a-contract-source-20261125/after-green.xml
# After source fix: 4 passed, exit 0

/home/dell/workspace/.worktrees/projectflow-visual-qa-73/.venv/bin/python -m pytest -q \
  tests/test_integration_contract_probe.py \
  tests/test_cross_repo_integration.py tests/test_real_merge.py \
  tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py \
  tests/test_workflow_decision_ux.py \
  --junitxml=docs/visual-qa/evidence/issue-73/pf-03a-contract-source-20261125/targeted.xml
# 51 passed, 0 failed/errors/skipped, exit 0
```

The combined run exercises merge verification, task decision behavior, and the visual contract/execution tests. No provider or network service was called. Full suite, build/preflight/smoke, and CI were not run for this narrow source change.

## Source identity and safety

- PR #74 expected remote head before this work: `4164afde07276ebc07de72f22c89fcd289f0a1ad`; fetched `origin/agent/codex/visual-qa-73` still matched at pre-publication check.
- Base: `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`.
- Local task branch at test time: `agent/codex/issue-73-engineering-fixture-20261125`, parent `4c5987f8ac4df27e7350db61c1b7ff1c541c4450` (tree `803891f39fe1d6161f933f491b4a84b6ec1cec9c`).
- No ProjectFlow main/WIP, NovaRetail, production runtime, Docker daemon, CI setting, or provider credential was changed.
- Rollback for the source change is a single revert of the published PF-03a commit; temporary Git repositories were pytest-owned under the system temp directory and removed by pytest.

## Gates and follow-up

- PF-03a targeted integration contract source: **PASS locally**.
- Gate A full suite: unchanged, **FAIL** until a qualifying full-suite run passes.
- Gate B/C approved equivalent Nova visual pilot: unchanged, blocked.
- Gate D opt-in/guide: prior local evidence only; this change does not alter it.
- Gate E exact-SHA CI: not established by these local tests.
- Gate F isolation: PASS for this worktree; preserved dirty main and other WIP.

Remaining independent PF-03b sequencing/evidence risks and PF-02b provider authorization were not changed or tested here.
