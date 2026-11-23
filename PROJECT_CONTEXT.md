# Project Context — P0 visual QA issue #73

Updated 2026-11-24. Recovery remains open; this is not a completion claim.

## Worktrees and safety

- ProjectFlow: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`, evidence/context updates are committed locally and synchronized to draft PR #74; use `git rev-parse HEAD` and the PR page for exact current SHAs. Preserve its two untracked evidence directories and do not mutate dirty `main`.
- NovaRetail: `/home/dell/workspace/.worktrees/novaretail-visual-reference-fixture-001`, branch `feature/nwr-visual-reference-fixture-001`; PR #160 remote head `c680931cda582e1effcf51726862404febef8b7e`. Its three permitted measured repair loops ended at source `a46e236d32e0b1d7d3b7594b3a82d8ec0ec72e5f`, local final tree `e500e77e0be0d645b41d84785ac761a4c37ffd37`; no more loops in this task.
- No production writes, deploys, merge, credential storage, or ProjectFlow main changes.

## Current outcome and evidence

- Original Nova design remains `docs/design/web-journey/06-order.png`, SHA-256 `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`. UIFix 1.2.6 pinned at `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53`; native 1586x992 DPR1 and fixed 8% threshold.
- Three repair-loop metrics: 17.110% baseline → 18.182% → 16.713% → **16.723% final (FAIL)**. Last loop degraded by 0.010 points vs loop 2; net improved 0.387 points vs baseline. No crop, mask, reference change, or threshold reduction. Root visual mismatch remains large concept differences in tabs, goods region, and totals/delivery area. Region metrics and actual/diff/overlay/metrics/report are committed in Nova PR `prototypes/baoyu/NWR-VISUAL-REFERENCE-FIXTURE-001/react-pilot/loops/`.
- Existing React preview route `/app/store-minh-anh/orders/new` and fixture-only concept data were tested. `OrdersLive` remains untouched. Five viewport Playwright checks passed with zero overflow, failed/off-origin/write requests, page errors, or console errors. Nova UI preview lint, 35 tests, and build passed; build retains existing large-chunk warning.
- Nova UI source/test head c680931 had Audit and Application failures; subsequent docs-only exact PR head `a417ba7a701dbaab35192fd74ed431eabe85bb02` had four completed push check-runs, all failed, plus a duplicate PR-event workflow queued at last query. Findings repeat: parity omissions `retail.issues`/`retail.store-settings`, four base-unchanged orphan projects, and shared-runner Docker IPAM exhaustion before browser/Postgres test steps. Runner owners were notified through PR #160 and issue #73 evidence.
- ProjectFlow full suite remains **19 failed / 1551 passed / 1 skipped, exit 1**, in `docs/visual-qa/evidence/issue-73/logs/full-suite-final-attempt.log`. Five provider-backed failures returned `Planner exited 1 ... subtype=unknown`; a separate single real Claude probe confirmed HTTP429 usage limit, but the five `unknown` outcomes are not proven to share that cause. Real Builder E2E timed out at 420s. Sandbox lifecycle, capacity/UX/state, soak, and verification failures are distinct. Eight selected UX/state failures pass on both the current branch and an extracted base snapshot; see `logs/base-targeted-ux-state-20261124.log`. No clean-base full-suite run exists, so other failures are not labeled as baseline or branch regressions. Earlier focused 55-test process exited 143 after 29 dots, without summary; cause unknown. Do not claim full suite pass.
- ProjectFlow preflight/build/smoke and provider-independent visual contract checks have prior recorded passes; Vietnamese copy/paste guide is `docs/VISUAL_QA_HUONG_DAN.md`. Opt-in remains UI-only and TaskDecisionService remains authoritative.
- Added `tests/__init__.py` after isolated execution showed an installed Terminal MCP `tests` package shadowed this repo's `tests.conftest`; verified import now resolves to the worktree. Eight previously failing UX/state tests pass in a combined isolated rerun, including the previously import-blocked closed-sandbox test. After this change preflight/build/smoke passed (smoke: 1 test); full suite was not repeated and remains a failure. No clean-base full-suite comparison, so unresolved failures are not classified as branch regressions.
- PR #74 latest exact remote head has zero workflow runs and zero check-runs. Zero is not PASS. Current local ProjectFlow branch status contains only the two preserved untracked evidence directories plus committed evidence-only updates.

## Next actions

1. Keep both PRs draft/open: visual threshold, full suite, and exact-SHA CI are not accepted. The requested three Nova visual loops are exhausted.
2. ProjectFlow owners should fix the unclassified full-suite/provider/Builder failures and run the complete suite; five selected UX/state cases passed singly but are not proven baseline or branch defects.
3. Shared runner/repository owners should restore Docker IPAM capacity and resolve the existing Nova parity/build-graph inventory. Preserve all artifacts; rollback only by reverting task-branch commits.
