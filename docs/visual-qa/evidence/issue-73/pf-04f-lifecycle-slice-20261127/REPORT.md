# PF-04f — integrated deterministic lifecycle slice

## Identity and execution

- Exact PR #74 source and tested HEAD: `220e09bf1260903c2f1d1a7c56cdd84e0a691f0f`; tree `4b24d8575784f96fd2d0fb441db52e68213c9a6f`; base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. Local task worktree was clean before and after; remote branch matched this HEAD before the run.
- Python 3.14.4 / pytest 9.2.0 from the existing venv. Start/end: `2026-11-27T09:24:02Z`–`09:24:15Z` (`16:24:02`–`16:24:15` ICT).
- Exit 0; JUnit: 7 tests, 7 passed, 0 failures/errors/skips. No module or case was deselected.
- Environment: restricted `PATH=/usr/bin:/bin`, `DOCKER_CONTEXT` unset, `DOCKER_HOST=unix:///tmp/pf-04f-no-docker.sock`, task-private `TMPDIR` and pytest basetemp. No provider CLI, browser, Docker, shared resource, or production service was used.

## Exact selection

```sh
env -u DOCKER_CONTEXT PATH=/usr/bin:/bin TMPDIR="$TASK_TMP" \
  PYTHONPATH="$TASK_WORKTREE:$TASK_WORKTREE/tests" \
  DOCKER_HOST=unix:///tmp/pf-04f-no-docker.sock \
  "$TASK_VENV/bin/python" -m pytest -p no:cacheprovider -q -ra \
  tests/test_spec_lifecycle.py::test_requirement_analysis_creates_traceable_task \
  tests/test_spec_lifecycle.py::test_ready_proposal_writes_canonical_spec_and_real_registry_loads_it \
  tests/test_architecture_design.py::test_architecture_analysis_structured_result \
  tests/test_architecture_design.py::test_technical_design_incorporates_approved_architecture \
  tests/test_test_design.py::test_test_design_structured_result \
  tests/test_test_design.py::test_review_pass_approves_test_case_set_and_plan \
  tests/test_engineering_lifecycle_ui.py::test_control_surface_endpoint_composes_every_tab \
  --basetemp="$TASK_TMP/pytest" --junitxml=lifecycle.xml
```

The integrated UI test's `seeded_change` drives Requirement Analysis → spec proposal/review/apply → architecture analysis/review → technical design/review → test design/review → plan, then the test exercises the real Engineering Control Surface aggregation endpoint. The stage responses are deterministic test-local fake invoker output. The actual application services, validators/registry, SQLite, temporary Git repository, work products and UI aggregation execute; this is not a real model/provider run.

## Historical failure mapping

From the historical ledger at `golden-timeout-repair-20261125/failure-fingerprints.tsv`, these selected failed nodes/groups now passed on the integrated current source: requirement-analysis traceable-task (`other_assertion_or_contract_failure`, one node); architecture structured analysis (provider/planner outcome mismatch, one node); test-design structured result (provider/planner outcome mismatch, one node); test-design review-pass path (one node in the missing-test-design group); technical design with approved architecture (one node in the missing-architecture group); and the Engineering UI control-surface node (one node in the HTML-response-as-JSON cascade group). The selected control-surface test exercises the complete seeded lifecycle, not every one of the 20 historical cascade nodes. `test_ready_proposal_writes_canonical_spec_and_real_registry_loads_it` was an additional deterministic registry check.

This bounded selection does **not** qualify unselected historical nodes or resolve the full ledger. Other nodes in those groups and all unrelated historical failure groups remain unqualified on this exact integrated head. No new failure appeared in this selection.

## Classification and gates

These are deterministic fixtures, not provider acceptance: fake invoker output is injected at the resolver/runner boundary; the app/service/DB/Git/registry/UI paths remain real and disposable. There is no code change in this milestone. Gate A remains unqualified because this is 7 selected tests, not the full suite. Exact-SHA CI was not triggered by this run; E remains not PASS. No changes to B/C/D/E/F acceptance.
