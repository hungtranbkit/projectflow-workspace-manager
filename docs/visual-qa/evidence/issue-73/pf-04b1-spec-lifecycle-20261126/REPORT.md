# PF-04b1 — deterministic spec lifecycle fake-runner seam

Date: 2026-11-26 (Asia/Bangkok). Scope: `tests/test_spec_lifecycle.py` only; no production code or provider calls.

## Identity and ownership

- Published PR #74 head at start and reproduction: `47abf6f147e5f91910b2e4684631adbee2fa9425`; tree `9c8fd6340e5237929ec39c1c188b560ded81ecde`; parent/base candidate `78e4ddf8d35036e3511163ff2b6e996e00b3a486`. The source/evidence update was published as `d7fa2f0cfa0765efa1a4ec2ee0975befaf5085bd` (tree `581b2a9af01d7449d8d4da304296f8f8c84fa905`, parent exactly the verified PR head); PR head was verified at that SHA after publication.
- Current task worktree began clean at local `fd007d400145d73ef603590cf73159dd2b2b7552`; its tree exactly matched PR head. The test changes were run in that tree-equivalent worktree; the base reproduction used a detached clean checkout at the exact parent above. The base and PR commit objects were fetched/read without moving any branch.
- Python 3.14.4; pytest 9.2.0; existing task venv; no repo dependency changes. For the lifecycle module, `PATH=/usr/bin:/bin` excluded Claude. Disk remained ample. Main checkout and unrelated WIP were not modified.

## Root cause, reproduced on base and candidate

`PlannerAgentInvoker.invoke()` resolves `which("claude")` before calling its injected subprocess runner. The E5 lifecycle test helpers replaced only `.runner`; with `PATH=/usr/bin:/bin`, the real resolver returned no executable, so the deterministic fake never ran. Requirement analysis returned JSON `EXECUTION_FAILED`; the happy-path helper only checked HTTP 200, then proposal creation correctly returned an HTML 409 because no Requirement Analysis existed. This reproduced on both base and candidate, so it was a deterministic test-fixture defect, not a new PR regression.

Three representative red reproductions, same interpreter and restricted PATH on both trees:

| Node | Base result | PR candidate result |
|---|---|---|
| `test_valid_spec_proposal_created` | 1 failed: proposal endpoint HTML 409, missing requirement analysis | same |
| `test_requirement_analysis_structured_result` | 1 failed: outcome `EXECUTION_FAILED`, expected `READY` | same |
| `test_what_decision_requires_human` | 1 failed: expected human decision absent | same |

Per-tree logs and JUnit are `repro-*-red.{log,xml}`. These results reproduce the mechanism behind the historical 23-node missing-analysis fingerprint; after the fix the complete deterministic module passed. The unrelated historical fingerprints are not attributed to this cause.

## Scoped repair

The test-only `set_fake_runner()` now injects both `invoker.which` (a test-only executable path) and `invoker.runner`, and asserts that Planner, Requirement Analysis, Spec Author, and Spec Review share the same invoker object. All custom fake-runner assignments in this module use this helper. The positive `do_analyze()` helper now asserts outcome `READY`, so failed prerequisite analysis is reported at its source rather than as a downstream HTML/JSON error. Added one fail-closed regression: when executable resolution reports Claude unavailable, requirement analysis returns `EXECUTION_FAILED`, creates no work product, and does not call the runner. The real-provider test `test_real_spec_lifecycle_end_to_end` is unchanged and was not executed.

Changed file blob: base `1b830e2e53ce5fa2d2278f61927421cc07861331`; tested candidate `0fc734168ab7afc0bc6e30b02a1b58b45c510d4a`.

## Verification

All command outcomes below were captured on 2026-11-26 ICT; the raw pytest log tail records durations and counts. Exact replay (replace `$WORKTREE` and `$TEST_VENV` with the local checkout and existing venv paths):

```sh
PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  "$TEST_VENV/bin/python" -m pytest -q -vv -ra \
  tests/test_spec_lifecycle.py -k 'not test_real_spec_lifecycle_end_to_end' \
  --junitxml=spec-lifecycle.xml
```

Result: exit 0; 38 passed, 1 explicitly deselected real-provider test, 0 failures/errors/skips among selected tests; 44.46 seconds. JUnit: `spec-lifecycle.xml` (38 tests, 0 failures/errors/skips).

```sh
"$TEST_VENV/bin/python" -m pytest -q -vv -ra \
  tests/test_planner_service.py::test_malformed_envelope_produces_execution_failed_no_plan_row \
  tests/test_planner_service.py::test_planner_reports_allowlisted_provider_usage_limit_without_raw_payload \
  tests/test_planner_service.py::test_malformed_inner_result_produces_output_invalid_no_plan_row \
  tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py \
  --junitxml=provider-visual-regression.xml
```

Result: exit 0; 20 passed, 0 failed/errors/skips; 4.45 seconds. The three planner envelope tests use injected fake runners; no provider subprocess was invoked. The new missing-provider test separately proves fail-closed before runner invocation. JUnit: `provider-visual-regression.xml` (20/0/0/0).

Before-fix evidence: representative test invocations exited 1 on both base and candidate (candidate JUnit 1/1 and 2/2 failed; base same). `git diff --check` exited 0. No full suite, live provider, runtime/UI test, CI, or Nova lane ran in this milestone. Historical Gate A remains unqualified by this targeted run.

## Replay artifacts and hashes

This folder includes sanitized red/green logs, JUnit XML, and source blob hashes (the reviewed source diff is the commit diff). `SHA256SUMS.txt` lists literal file hashes and intentionally does not hash itself. Commands and paths are portable; host-specific root paths are redacted. Raw temporary reproduction checkouts/logs are task-owned and can be removed after publication.

## Gate and next step

PF-04b1 deterministic spec lifecycle seam: PASS (38 deterministic cases plus provider/visual regression 20 pass). Real-provider authorization remains absent; the real-provider test was not run. The commit updates the existing draft PR only; no merge or CI activation. This does not change A–F overall status: A full suite remains FAIL/not rerun; B/C remain blocked by approved Nova reference/acceptance; D retains prior local evidence only; E is not PASS (PR #74 workflow disabled/no exact-head checks); F preserved. Next lane is separately authorized PF-04b2 only after checking its owner/scope; do not invoke a provider without authorization.
