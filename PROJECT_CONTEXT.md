# Project Context — ProjectFlow visual QA issue #73

Updated 2026-11-24 ICT. Recovery is open; this is a handoff, not a completion claim.

## Scope and safety

- ProjectFlow task worktree: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`; local HEAD `2805bcd37fa9f183f7e38a887fc8891884051961` remains dirty with WIP. Do not reset, clean, or overwrite it. Draft PR #74 now points at `cee006ca53eb36b7be29cf654fb3081844c4cce1` (fast-forward from its prior remote head `6264da0df8787ff747cb9955bad8446415695768`). Code publication and verification used the GitHub connector plus a detached exact-SHA checkout. Preserve dirty `main`, all WIP, and remote history.
- No production writes, merge/deploy, force push, shared Docker cleanup, or workflow bypass occurred. Local `gh auth status` reports its token invalid; GitHub connector is the safe publication route.
- NovaRetail is separate: PR #160 exact head `0f1f616b14d919019cd1299954f179f2fc11bbb4`, POS proposal is unapproved fixture-only at 5.784358%, original legacy order screen 11.524542% and `REFERENCE_NOT_EQUIVALENT`. Do not claim product visual acceptance or alter its reference in this ProjectFlow-only turn.

## ProjectFlow change and verification

- `tests/test_visual_qa_runner_subprocess.py` exercises actual `TestRunner.start()`, `TaskDecisionService`, temporary SQLite, the default host subprocess command runner, local git fixture, loopback server and pinned UIFix/Chromium. It covers exact positive, intentional color negative, wrong dimensions, missing ref, unavailable server, backend opt-out and stale-SHA evidence. The real visual stage is dispatched; this does not invoke Codex/Claude generation providers.
- `pyproject.toml` has `pythonpath = [".", "tests"]`. With the repository `tests/__init__.py` marker, the old root-only pythonpath reproduced 11 collection errors because legacy tests import `conftest` and sibling test modules by top-level names. The updated exact-SHA collect-only probe sums to 1,575 items without import errors or weakened assertions. Full `./scripts/test.sh` on exact remote SHA `cee006ca53eb36b7be29cf654fb3081844c4cce1` exited 0 with 1,575 passed and 1 skipped in 3,137.38s. The collection sum (1,575) and runtime outcome total (1,576) differ by one and remain unresolved; see the exact-SHA evidence folder. No attempt was made to hide the existing skip.
- Targeted command (authorized browser wrapper and existing Playwright package): `CHROME_PATH=<authorized-chromium-wrapper> VISUAL_QA_PLAYWRIGHT_FROM="$PWD" .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py tests/test_visual_qa_runner_subprocess.py` — 18 passed, exit 0.
- Fixed the test-only Docker IPAM exhaustion without changing daemon configuration: `tests/conftest.py` assigns a route-checked unique `/28` from test pool `10.252.0.0/16` to each temporary sandbox Compose project through its own `.env`, then calls the existing real Compose runtime method. Direct NGINX Compose fixtures use that test-only network contract. The 14 formerly failing sandbox node IDs pass on the exact SHA. After the full suite, read-only inventory returned to 29 networks and zero `wm-*` containers/networks. No daemon config, production, or unrelated shared Docker state was changed.
- Subprocess evidence with real local command execution is in `docs/visual-qa/evidence/issue-73/runner-integration-subprocess-20261124-02/`; the fixture uses 0 threshold only to prove exact dispatch and failure semantics, not Nova's 8% product gate.
- Latest full log is sanitized and stored at `docs/visual-qa/evidence/issue-73/full-suite-runtime-20261124/full-suite.log`. Replay and guide are in `docs/VISUAL_QA_HUONG_DAN.md`.

## Gate state and blockers

- A PASS locally on exact PR source SHA: `./scripts/test.sh` exit 0, 1,575 passed / 1 skipped; Docker IPAM fixed in test fixtures. The collect-only count is one less than runtime outcomes and is still being investigated. Exact-SHA GitHub CI is a separate E gate.
- B FAIL: approved Nova legacy reference mismatch 11.524542%; POS proposal 5.784358% is unapproved fixture-only.
- C FAIL for acceptance: React preview interactions/responsive smoke were previously exercised, but approved equivalent visual reference is absent.
- D partial: UI-only opt-in/guide and actual ProjectFlow command-runner visual stage are verified; provider-backed image-to-code runtime is unverified.
- E NOT PASS: PR #74 Actions workflow is manually disabled (zero runs); Nova exact-head checks failed. A maintainer must authorize/re-enable ProjectFlow workflow; do not bypass.
- F PASS so far: task changes stayed isolated, no production/shared-runner/main mutation.

## Next actions

1. Diagnose why collect-only sums to 1,575 but pytest runtime reports 1,575 passes plus one skip, without changing test selection or assertions.
2. Ask the ProjectFlow maintainer to authorize/re-enable the disabled required Actions workflow and run checks on exact PR #74 HEAD `cee006ca53eb36b7be29cf654fb3081844c4cce1`; E remains blocked until checks run and pass.
3. Continue independent Nova visual equivalence/provider work in its existing owned scope; keep POS proposal unapproved and legacy order reference `REFERENCE_NOT_EQUIVALENT`.

## Latest exact-SHA handoff

- Evidence: `docs/visual-qa/evidence/issue-73/full-suite-runtime-20261124-ipam-fix/REPORT.md`, logs, host inventory, and `SHA256SUMS.txt`.
- Full suite on exact clean detached checkout: 1,575 passed, 1 skipped, exit 0. Preflight/build, 14 recovered sandbox tests, 18 visual runner tests, CLI 4/4, and smoke passed. The runtime integration remains independent of external Claude/Codex provider invocation.
- PR #74 has zero workflow runs at `cee006ca...`; the workflow remains disabled. No CI bypass or maintainer setting change was made.
