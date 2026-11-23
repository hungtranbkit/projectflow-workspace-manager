# Project Context — Issue #73 visual QA P0

## Current state

- ProjectFlow task worktree: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`, local base lineage `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. Draft PR #74: https://github.com/hungtranbkit/projectflow-workspace-manager/pull/74. Dirty main checkout `/home/dell/workspace/projectflow-workspace-manager` was not touched.
- Visual QA integration stays opt-in via `VISUAL_QA_REQUIRED: true`, scoped to UI tasks. `TaskDecisionService` remains the single gate decision source. CLI and pinned UIFix 1.2.6 are in `tools/visual_qa/`; Vietnamese copy/paste guide is `docs/VISUAL_QA_HUONG_DAN.md`.
- NovaRetail has a separate preview-only branch and draft PR #160: https://github.com/hungtranbkit/novaretail-web/pull/160. The existing React `RetailApp` has a fixture-port-only deterministic new-order route; connected/API ports still use the existing `OrdersLive`. No second production app, API/data writes, or deployment.

## Verified results and blockers

- ProjectFlow quick preflight/build/smoke, visual CLI smoke (exact, deliberate negative, repaired, missing/dimension/server fail-closed) and scope/planner classification tests passed in prior recorded runs.
- Full `scripts/test.sh` remains **FAIL**: 19 failed, 1551 passed, 1 skipped, exit 1, 2648.21 s. Four live provider planner/review/spec/test-design paths report planner exit 1; a bounded real Claude probe returned HTTP 429 `usage_limit_reached`. The real Builder worktree E2E exceeded 420 s without fixing `calc.py`. A targeted sequential rerun of five affected UX/runtime files ran 45 tests: 37 passed, 8 failed. The sandbox failures correlate with retained Docker operation stderr `all predefined address pools have been fully subnetted`; host had 30 unrelated networks and no task `wm-*` networks, so none were deleted and daemon config was not changed. Do not attribute all full-suite failures to provider quota.
- NovaRetail Playwright E2E passed at 1586x992, 430x932, 768x1024, 1366x768 and 1920x1080; no overflow, page/console/failed/off-origin requests or writes. Preview lint passed, 27 unit tests passed, build passed. UIFix against unchanged `06-order.png` SHA `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`, native 1586x992, unchanged 8% threshold: after repair **19.753%, FAIL**. SHA-pinned after artifacts are at `docs/visual-qa/evidence/issue-73/novaretail-react-preview/final/`; full gate report is `docs/visual-qa/evidence/issue-73/RECOVERY-20261123.md`.
- Exact-SHA CI at ProjectFlow PR head `c57d05a3dd9c22b73dc9580a2c8077f0875ffda6`: zero runs and zero statuses. Nova PR head `949bb03d2e29c78ee309d345465bd0285ea29669`: zero runs and zero statuses, `mergeable=false`. Neither is PASS. Local `gh auth status` says token invalid; connected GitHub API was used for Nova branch/PR writes with expected-SHA checks.
- NovaRetail `TASKS.json` task remains BLOCKED under its original static-only scope; an active audit task owns `TASKS.json`. Do not edit the task registry or claim registry acceptance without the ownership window. Nova current handoff is in its `PROJECT_CONTEXT.md`.
- No production or database mutation/deployment occurred. No credentials are stored in project files. Exact gate evidence and next actions are in `docs/visual-qa/evidence/issue-73/RECOVERY-20261123.md`.

## Next actions

1. Repair real React/CSS visual mismatch until UIFix <=8%, keeping the reference and threshold fixed; rerun browser evidence.
2. Diagnose and fix the full-suite 19 failures, resolve provider quota and Builder timeout, then rerun the entire ProjectFlow suite.
3. Resolve Nova task scope/PR mergeability; sync ProjectFlow report and artifacts to PR #74, trigger exact-SHA CI and require actual passing results before declaring completion.
