# PF-05b — integrated visual verification must require fresh evidence

Date: 2026-11-27 (ICT). This is a bounded ProjectFlow release-path repair on the existing draft PR #74. It does not qualify a browser run or any overall A–F gate.

## Identity and scope

- PR branch expected before work: `agent/codex/visual-qa-73`, head `bfccb97f6a944806b397b8a635c0a61209cdad61`.
- Existing isolated checkout began at local commit `2f887ebfb010221b0a89e4c132407c211114c82d`; its tree `569f5aca021bb9214f3f0e1798e32d03c329b90c` matched the fetched remote head's tree exactly. The local commit object is not the remote commit object; tests ran on a tree-equivalent checkout plus this task's worktree changes.
- The source/test candidate tree used for the final green run is `06f2b53ebdd1987d7454a26609a86fd6475e98b0`. Final published source/evidence commit identity is recorded in the PR checkpoint after publication.
- Changed implementation: `app/services/integration_service.py` and `app/main.py`; regression coverage: `tests/test_integration_contract_probe.py`.

## Confirmed false-positive and repair

Red used a real temporary Git repository with a committed enabled visual contract, a valid synthetic 320×240 PNG, and real subprocess command `true`. The test failed because `verify_integrated_state()` returned `passed=True` when both declared commands exited 0 although the visual command produced no evidence. Red: 1 test, 1 failed, 0 errors/skips, exit 1; 2026-11-27 10:39:51–10:39:53 UTC (17:39:51–17:39:53 ICT). The failure shows the old implementation's false PASS.

The visual stage now uses the same `SandboxedCommandRunner` instance wired into `TestRunner` by `create_app`; it has no fallback to the legacy argv runner. In required-auth mode that object remains mandatory and fails closed on sandbox startup errors. The service reads the contract/reference from the exact integrated Git probe through `load_committed_visual_qa_contract`, allocates a private fresh run directory and run ID, passes the tested SHA/run identity/output location to the command, checks source cleanliness and HEAD before/after, then calls the existing `validate_visual_qa_run` twice (the second read pins the manifest digest). Missing, stale, tampered, or malformed output fails the integration stage. Temporary run evidence is removed after validation; the result records the validated digest/run ID, not a new status authority or a `test_runs` row. Nonvisual stages retain the existing runner and behavior.

The suite also asserts `create_app()` passes the identical configured `SandboxedCommandRunner` instance to `IntegrationService` and `TestRunner`. The positive fixture uses existing synthetic evidence helpers solely to test the validator contract. It is **not** a Chromium/UIFix run and does not count as visual pilot evidence.

## Verification

- Red command: `python -m pytest -x -vv tests/test_integration_contract_probe.py::test_required_visual_integration_does_not_pass_from_exit_zero_without_evidence --junitxml=<task-artifact>/red.xml`; exit 1 as above.
- Green command: `python -m pytest -q -ra tests/test_integration_contract_probe.py tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_task_decision_required_stages.py --junitxml=<task-artifact>/green.xml`; 45 passed, 0 failed/errors/skips, exit 0, 2026-11-27 10:58:05–10:58:24 UTC (17:58:05–17:58:24 ICT), Python 3.14.4 / pytest 9.2.0.
- Test environment: restricted `PATH=/usr/bin:/bin`; task-private HOME/TMPDIR; `DOCKER_HOST=unix:///tmp/pf05b-nonexistent-docker.sock`; `DOCKER_CONTEXT` empty. No browser, Docker daemon, provider, external requests, production app, real repository merge, or deploy was used.
- `git diff --check`: pass. JUnit: `green.xml` 45 tests, 0 failures/errors/skips. Raw process outputs were retained outside the repository at `/home/dell/workspace/task-artifacts/issue-73-pf05b-integration-visual-evidence-20261127/`; committed copies here have host paths/hostname sanitized. Their hashes cover the published sanitized bytes.

## Separate release-safety finding

`integrate_task()` still updates the target branch ref before calling `verify_integrated_state()`. This task deliberately does not change merge sequencing. A failed post-update verification can therefore leave the target ref advanced and return `INTEGRATION_VERIFY_FAILED`; this is a separate release-safety follow-up requiring its own transaction/rollback design and review.

## Gate status and rollback

No overall gate is upgraded. Browser execution and exact-SHA CI remain unavailable/unqualified; the synthetic positive is only deterministic contract coverage. PR #74 stays draft. Rollback is limited to reverting this PF-05b commit on the existing task branch; no production/main/WIP/shared daemon state was changed.
