# PF-04b13 — Task Visual QA panel

Date: 2026-11-26 (Asia/Bangkok). This is a focused UI milestone, not completion of issue #73.

## Scope and source identity

- Repository: ProjectFlow; PR #74 remains draft.
- Exact remote base at preflight: `2827b2ba32a6e9f5f84ddc9beb74b846779754d0`, tree `da59c305a27bceddb746ff308938e821a6be383e`.
- Local starting commit: `ffe2165734835006413a6e285edb0b05a98e89d4`, same tree. The remote branch advanced from `7fad14ffe02dd8303a59a4e00ac31344e31cfbfd` to the exact head above during fetch; its tree matched the local starting tree. Changes were made only in the task-owned worktree/branch.
- The implementation reuses `TaskDecisionService.builder_view()`, its existing evidence verifier/status, `TestRunner` endpoint, and authorized `/test-runs/{id}/log` route. No new gate/status/service, artifact download endpoint, upload, or arbitrary command/URL field was added.
- Opt-out explains that repository configuration alone does not turn on the gate. Opt-in displays contract/reference digests, route, threshold, viewport and tested SHA. Approval is explicitly “not recorded.” Only metrics returned by successful evidence validation are rendered. Missing, invalid, and stale evidence remain explicit and fail closed.
- Modified paths: `app/services/task_decision_service.py`, `app/services/visual_qa_evidence.py`, `app/templates/task_detail.html`, `tests/test_visual_qa_evidence.py`, `tests/test_visual_qa_task_panel.py`, `docs/VISUAL_QA_HUONG_DAN.md`, `PROJECT_CONTEXT.md`.

## Verification actually run

Command (task-local Python 3.14 venv; task-local nonexistent socket prevents accidental Docker access):

```sh
DOCKER_HOST=unix:///tmp/projectflow-vq-panel-no-daemon.sock \
  /tmp/pf-vq-panel-venv-20261126/bin/python -m pytest \
  -p no:cacheprovider -q -ra \
  --junitxml=docs/visual-qa/evidence/issue-73/task-visual-qa-panel-20261126/focused-regressions.xml \
  tests/test_visual_qa_task_panel.py \
  tests/test_visual_qa_evidence.py \
  tests/test_visual_qa_contract.py \
  tests/test_visual_qa_execution.py \
  tests/test_task_decision_required_stages.py \
  tests/test_test_runner_source_identity.py \
  tests/test_b1_read_isolation.py \
  tests/test_b03_authz.py \
  tests/test_engineering_lifecycle_ui.py::test_existing_task_detail_page_still_works
```

- Start/end: 2026-11-26 07:30:55–07:31:48 UTC (14:30:55–14:31:48 ICT).
- Exit: `0`; JUnit: `95` tests, `0` failures, `0` errors, `0` skipped; duration `49.616s`.
- Coverage includes opt-in/off, current valid run metrics, stale run, missing/invalid contract, absent evidence digest, tampered evidence, escaped Task title, existing authz/decision/stage/source identity regressions.
- Evidence fixtures are deterministic test data validated through the actual verifier. They are not a live provider/browser pilot or product-design approval.
- `git diff --check` passed before evidence finalization.

## Boundaries and remaining work

- No browser screenshot was run: the selected task venv does not contain the supported Playwright package. The actual FastAPI route/template was tested with `TestClient`.
- No full suite, provider invocation, Docker daemon, shared runner, production, CI settings, merge, or deploy was used.
- This milestone does not change issue #73 gates. Full suite/provider authorization, approved Nova reference/pilot, exact-SHA CI, and PF-04b10 incident disposition remain unresolved. PR #74 remains draft.

## Replay artifacts

- `focused-regressions.log` — sanitized pytest output.
- `focused-regressions.xml` — JUnit result.
- `run-summary.json` — source identity, command summary, times, exit/counts and log/XML digests.
- `SHA256SUMS` — hashes of the report, log, JUnit and summary (manifest excludes itself).
