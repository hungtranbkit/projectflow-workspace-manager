# ProjectFlow full-suite recovery and real visual-stage dispatch — 2026-11-24

Status: **A FAIL; D runtime-stage integration verified; E blocked.** No production DB/service, shared Docker resources, or dirty `main` were modified.

## Exact full-suite run

- Worktree: task-owned `.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`.
- Source HEAD at run start: `2805bcd37fa9f183f7e38a887fc8891884051961`; worktree included the documented uncommitted changes for the visual-stage integration and `pythonpath` fix. This is not a claim that that dirty tree equals a commit SHA.
- Command: `CHROME_PATH=<authorized-chromium-wrapper> VISUAL_QA_PLAYWRIGHT_FROM="$PWD" ./scripts/test.sh`.
- Result: exit 1, **14 failed / 1561 passed / 1 skipped**, 2795.43 seconds (46:35), 12,182 warnings. Full sanitized output: [`full-suite.log`](full-suite.log).
- The previous five provider-backed failures did not recur in this run. Their prior `subtype=unknown` output did not establish quota as their cause. The current 14 failures are all caused by Docker sandbox provisioning failure and downstream state assertions.

## Reproduced sandbox root cause

Read-only inspection of pytest-owned SQLite databases under `$PYTEST_TMP/` found failed `sandbox_operations` rows. For example, `test_reset_data_does_not_touch0/test.db` records `operation_type=PROVISION`, `status=FAILED`, `exit_code=1`; `stderr_tail` contains:

```text
Error response from daemon: all predefined address pools have been fully subnetted
failed to create network wm-reset-b-agent_workspace-2-b6384f_default
```

`test_sandbox_running_displays_0/test.db` records the same error for `wm-verif-b-agent_workspace-1-778d39_default`. Other failed test databases show the same daemon response. Read-only SQLite checks confirmed the same IPAM error for all 14 failing test databases. Docker read-only inspection after the run reported `.DefaultAddressPools = null`; 29 networks were present. No network/container/volume cleanup was run. Consequential UX/task decision failures (FAILED sandboxes, missing runtime URLs, `REBUILD_SANDBOX`) are downstream of this provision failure. This does not prove every assertion is correct on a healthy daemon; it proves these failures did not reach the intended runtime state on this host.

## Deterministic collection failure fixed

With the local `tests/__init__.py` package marker present, `pytest --collect-only -q -o pythonpath=.` reproduces **11 collection errors / exit 2** because legacy modules import `conftest` and sibling modules by top-level name. `pyproject.toml` now uses `pythonpath = [".", "tests"]`; default collection succeeds for all **1,572 tests**. This fixes collection without changing assertions or suppressing tests.

## Actual ProjectFlow command-runner visual stage

`tests/test_visual_qa_runner_subprocess.py` drives the real `TestRunner.start()` with temporary SQLite, the default host subprocess runner, a disposable local git fixture and loopback server, and pinned UIFix/Chromium. It tests actual command exit/status and `TaskDecisionService` SHA binding. Evidence: [`runner-integration-subprocess-20261124-02`](../runner-integration-subprocess-20261124-02/report.md).

- Targeted command: `CHROME_PATH=<authorized-chromium-wrapper> VISUAL_QA_PLAYWRIGHT_FROM="$PWD" .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_visual_qa_runner_subprocess.py`.
- Result: **18 passed, exit 0** (14 contract + 3 dispatch/decision + 1 subprocess integration test).
- True subprocess outcomes: exact local fixture PASS; controlled UI color mismatch FAIL/exit 1; wrong dimensions fail closed/exit 2; missing reference is rejected and TaskDecisionService says `NOT_RUN`; unavailable loopback server fail closed/exit 2; backend without marker opts out; evidence bound to old commit returns `NOT_RUN` at new HEAD.
- The synthetic fixture's threshold 0 tests exact dispatch behavior only; it is not the product's fixed 8% NovaRetail visual gate and not an approved design-equivalence claim. No external requests were observed; only local GET requests.

Replay:

```sh
CHROME_PATH=/path/to/authorized-chromium-wrapper \
VISUAL_QA_PLAYWRIGHT_FROM="$PWD" \
.venv/bin/python -m pytest -p no:cacheprovider -q \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py \
  tests/test_visual_qa_runner_subprocess.py
```

## Remaining blockers / ownership

- The 14 tests need a Docker daemon with usable address pools. This Dell daemon reports no configured default pools and Compose fails network creation. Maintainer/runner owner should provide a healthy isolated test daemon or restore a bounded pool configuration after reviewing host networks; do not prune shared Docker state. Once available, rerun the exact full suite.
- PR #74 Actions workflow remains manually disabled; exact-SHA CI cannot pass until a maintainer re-enables/authorizes the workflow. `gh auth status` also reports the local CLI token invalid; connector writes may be used without exposing credentials.
- Provider runtime generation was not exercised by the subprocess integration; real provider behavior remains separate and unverified by this test.
