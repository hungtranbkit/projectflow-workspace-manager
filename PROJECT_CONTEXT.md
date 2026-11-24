# Project Context — ProjectFlow visual QA issue #73

Updated 2026-11-24 ICT. Recovery is open; this is a handoff, not a completion claim.

## Scope and safety

- ProjectFlow task worktree: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`; local HEAD at latest verification `2805bcd37fa9f183f7e38a887fc8891884051961`, with uncommitted task changes. Preserve the existing dirty `main`, all WIP, and remote history. PR #74 is draft; last confirmed remote head before this handoff was `4d866b9855185c7dc1a0b7517a60e93017ecb00c`.
- No production writes, merge/deploy, force push, shared Docker cleanup, or workflow bypass occurred. Local `gh auth status` reports its token invalid; GitHub connector is the safe publication route.
- NovaRetail is separate: PR #160 exact head `0f1f616b14d919019cd1299954f179f2fc11bbb4`, POS proposal is unapproved fixture-only at 5.784358%, original legacy order screen 11.524542% and `REFERENCE_NOT_EQUIVALENT`. Do not claim product visual acceptance or alter its reference in this ProjectFlow-only turn.

## ProjectFlow change and verification

- `tests/test_visual_qa_runner_subprocess.py` exercises actual `TestRunner.start()`, `TaskDecisionService`, temporary SQLite, the default host subprocess command runner, local git fixture, loopback server and pinned UIFix/Chromium. It covers exact positive, intentional color negative, wrong dimensions, missing ref, unavailable server, backend opt-out and stale-SHA evidence. The real visual stage is dispatched; this does not invoke Codex/Claude generation providers.
- `pyproject.toml` has `pythonpath = [".", "tests"]`. With the repository `tests/__init__.py` marker, the old root-only pythonpath reproduced 11 collection errors because legacy tests import `conftest` and sibling test modules by top-level names. The updated config collects all 1,572 tests without weakening or skipping assertions.
- Targeted command (authorized browser wrapper and existing Playwright package): `CHROME_PATH=<authorized-chromium-wrapper> VISUAL_QA_PLAYWRIGHT_FROM="$PWD" .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_visual_qa_runner_subprocess.py` — 18 passed, exit 0.
- Full `./scripts/test.sh` ran 46:35 and failed: **14 failed / 1561 passed / 1 skipped**, exit 1. The 14 IDs are in `docs/visual-qa/evidence/issue-73/full-suite-runtime-20261124/REPORT.md`; pytest-owned SQLite `sandbox_operations.stderr_tail` confirms Compose network creation failed with `all predefined address pools have been fully subnetted`. Docker read-only inspection found `.DefaultAddressPools=null`, 29 networks. No shared Docker objects were removed. Other provider-backed failures in the old 19-failure log did not recur in this new run; no cause is inferred from their disappearance.
- Subprocess evidence with real local command execution is in `docs/visual-qa/evidence/issue-73/runner-integration-subprocess-20261124-02/`; the fixture uses 0 threshold only to prove exact dispatch and failure semantics, not Nova's 8% product gate.
- Latest full log is sanitized and stored at `docs/visual-qa/evidence/issue-73/full-suite-runtime-20261124/full-suite.log`. Replay and guide are in `docs/VISUAL_QA_HUONG_DAN.md`.

## Gate state and blockers

- A FAIL: full suite fails due Docker daemon IPAM exhaustion; requires owner-provided healthy isolated daemon/pool, then full rerun. Collection defect was fixed.
- B FAIL: approved Nova legacy reference mismatch 11.524542%; POS proposal 5.784358% is unapproved fixture-only.
- C FAIL for acceptance: React preview interactions/responsive smoke were previously exercised, but approved equivalent visual reference is absent.
- D partial: UI-only opt-in/guide and actual ProjectFlow command-runner visual stage are verified; provider-backed image-to-code runtime is unverified.
- E NOT PASS: PR #74 Actions workflow is manually disabled (zero runs); Nova exact-head checks failed. A maintainer must authorize/re-enable ProjectFlow workflow; do not bypass.
- F PASS so far: task changes stayed isolated, no production/shared-runner/main mutation.

## Next actions

1. Publish the scoped test/config/guide/evidence changes as a fast-forward to the latest PR #74 head using the GitHub connector; confirm exact remote SHA and keep PR draft.
2. Ask the Docker/runner owner for a bounded, isolated test daemon or restored address pool; then run `./scripts/test.sh` again and classify any remaining non-IPAM failures from fresh tracebacks.
3. Ask the ProjectFlow maintainer to re-enable/authorize the disabled Actions workflow and run checks on the resulting exact PR SHA. Keep A/E blocked until real full-suite and exact-SHA CI pass.
