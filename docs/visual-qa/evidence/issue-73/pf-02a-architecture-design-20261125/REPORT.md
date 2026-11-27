# PF-02a — architecture/design deterministic fake-runner seam

Date: 2026-11-25 (Dell, Asia/Bangkok). Scope is limited to test-only fixture wiring in `tests/test_architecture_design.py`; no production provider behavior changed and no NovaRetail files were touched.

## Exact baseline and ownership

- PR #74 was draft, base `main` at `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`, remote head `14e1aba3442a9296ccc82cdd6a505642689b7ff5` at the start of this task. The head is the PF-01 source commit plus handoff-only context update; task file `tests/test_architecture_design.py` was unchanged from base.
- The base and candidate reproductions ran from clean detached snapshots at those exact SHAs. Source change was developed in the existing task-owned branch/worktree, based on a local commit whose tree matched the published PR tree before this patch. Dirty `main` and separate WIP were preserved.
- For local verification the installed test venv reported Python 3.14.4 and pytest 9.2.0. Tests ran with `PATH=/usr/bin:/bin`, so `claude` was not resolvable. No Claude provider/auth/quota invocation was made.

## Reproduction and cause

The same two deterministic tests failed on both base and candidate:

- `tests/test_architecture_design.py::test_architecture_analysis_structured_result`
- `tests/test_architecture_design.py::test_technical_design_structured_result`

Observed requests returned HTTP 200 with `application/json`, but payload `outcome=EXECUTION_FAILED`, `work_product=null`, `message="claude CLI not found in PATH"`. The tests then expected `READY`.

Root cause is the test seam: `set_fake()` replaced `PlannerAgentInvoker.runner` but not `PlannerAgentInvoker.which`; production `invoke()` checks `which("claude")` before calling its injectable runner. The services share the same invoker instance (the new helper asserts that identity for architecture analysis/review and technical/UI/UX/design review). The deterministic fake was therefore not controlling the entire subprocess boundary. This is present on base and candidate; it is not a PR regression or production route defect.

A first partial helper correction exposed a second deterministic test with a direct custom runner assignment (`test_bounded_design_refinement_loop_never_infinite`). It failed with the same `EXECUTION_FAILED` outcome. The final correction routes every fake runner installation in this module through `install_fake_runner()`. The single intentionally real Claude end-to-end test does not call that helper and remains unchanged.

## Scoped change

`install_fake_runner(client, runner)` verifies the shared invoker identity, injects a sentinel executable resolver, then attaches the deterministic runner. The sentinel is consumed only by the fake runner; it is never executed. Existing response/business and negative design assertions remain intact. The module's one real-provider lifecycle test was not altered or run.

## Test evidence

Final local source commit tested: `51ed141588da4533abaa530bdcae37bd73cea92b`.

- `tests/test_architecture_design.py` deterministic tests plus `tests/test_claude_structured_turns.py`, `tests/test_visual_qa_contract.py`, `tests/test_visual_qa_execution.py`: **72 passed, 1 deselected, exit 0, 66.00s**. The one deselected item is the named real-provider end-to-end test; no test was marked skipped or modified. The structured-output failure-envelope tests passed.
- A direct local negative probe constructed `PlannerAgentInvoker(which=lambda _: None)` and a runner that would fail if called; the actual invoker raised `PlannerAgentError("claude CLI not found in PATH")` before runner invocation: **PASS, exit 0**. No external provider or network call.
- Base and candidate pre-fix architecture/design reproductions each produced **2 failures, exit 1** with the same JSON response for both endpoints.
- The 17 visual contract/execution tests passed within the 72-test combined run. No full suite, build, smoke, browser launch, or CI run was done.
- Test runner emitted existing Starlette/httpx and `asyncio.iscoroutinefunction` deprecation warnings; no assertion was weakened.

The exact commands, per-test names, HTTP probe, JUnit XML, exit ledgers, and read-only runtime preflight are in this folder. Paths and runner hostname are sanitized in tracked evidence. Local unredacted preflight/logs remain under the task artifact folder on Dell.

## Runtime and blockers

- Read-only preflight: `claude` resolves to a local executable at version 2.1.332 on the interactive host, but is absent from the restricted test PATH. Auth/quota status was not probed to avoid invoking the provider. Python 3.14.4, pytest 9.2.0, Node v26.7.0. Available task-owned UIFix runtime verified at pin `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53` (UIFix 1.2.6), with bundled Playwright 1.60.0/Chromium rev 1223. The tool's verify script passed; no browser was launched because this task is focused on Python test fixtures.
- Remaining provider-dependent test `test_real_architecture_design_lifecycle_end_to_end` was intentionally not run without authorization for a genuine provider invocation. No auth/quota claim is made.
- Full suite and PR CI remain unverified; Gate A stays FAIL and Gate E is not PASS. Do not infer the separate `test_test_design.py` or all requirement/design failure fingerprints share this cause without their own exact repro.

## Replay

From a clean ProjectFlow checkout with its test venv activated:

```bash
export PATH=/usr/bin:/bin
export PYTHONPATH="$PWD:$PWD/tests"
python -m pytest -vv -rs -p no:cacheprovider \
  tests/test_architecture_design.py tests/test_claude_structured_turns.py \
  tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py \
  -k 'not test_real_architecture_design_lifecycle_end_to_end'
```

The excluded real-provider test remains in source and must be run only on an authorized provider runner. This replay is targeted evidence, not full-suite Gate A.
