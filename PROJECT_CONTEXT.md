# Project Context — Issue #73 visual QA P0

## Current state

- ProjectFlow task worktree: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`, local branch contains the issue #73 implementation and evidence; GitHub connector creates remote commits with different SHAs. PR #74 remains draft and mergeable. Dirty main checkout `/home/dell/workspace/projectflow-workspace-manager` was not touched.
- Visual QA stays opt-in via `VISUAL_QA_REQUIRED: true`, scoped to UI tasks. `TaskDecisionService` remains the gate authority. CLI and pinned UIFix 1.2.6 are in `tools/visual_qa/`; Vietnamese guide is `docs/VISUAL_QA_HUONG_DAN.md`.
- NovaRetail draft PR #160 is now at remote head `2a326d84bbb7f031817403a9dbbcfbeb4590ad05`, mergeable against main `4c3d76b3020da75ce3edc872b6119dda82ce3a2e`. Local Nova head `4d851e83bb2a6099e5acb1fffd64a1159559ffcd` has identical tree; connector created remote merge commit with main parent. Exact Nova head has two workflow failures: prototype/contract audit plus clean app/Postgres/browser gates.

## Verified results and blockers

- ProjectFlow quick preflight/build/smoke, visual CLI exact/negative-color/repaired/missing/dimension/server fail-closed smoke, and prior scope/planner classification tests passed. Provider-independent Planner (excluding the real Claude subprocess test) + visual-qa contract rerun: **55 passed, exit 0**; retained at `docs/visual-qa/evidence/issue-73/logs/provider-independent-contract-rerun.log`.
- Full `scripts/test.sh`: **19 failed, 1551 passed, 1 skipped, exit 1, 2648.21 s**. Four provider-backed flows report planner subprocess `subtype=unknown`; bounded real Claude CLI probe returned HTTP 429 `usage_limit_reached`. Real Claude Builder E2E timed out at 420 s without fixing `calc.py`. Five-file UX/runtime rerun: 37 passed / 8 failed, with retained Docker IPAM stderr `all predefined address pools have been fully subnetted`; do not attribute every full-suite failure to quota/IPAM. No 143 process termination is in the final log; its `143 warnings` are warning counts. Codex CLI disposable smoke could not execute even `pwd` (exit 159) in workspace-write sandbox; no files changed, no unsafe bypass (details in `logs/codex-disposable-builder-probe-20261123.md`).
- Existing NovaRetail React preview route passes Playwright 1/1 across five viewports; zero overflow, off-origin/failed requests, console/page errors or write requests. UI preview lint, 35/35 unit tests and build pass (chunk warning). Against unchanged `06-order.png` SHA `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`, fixed 8% at 1586x992, reported before repair 36.333%, after scoped CSS repair 34.372%: visual fidelity remains FAIL / `REFERENCE_NOT_EQUIVALENT`. Current evidence is accessible via PR #160 and detailed in `RECOVERY-20261123.md`.
- The last checked ProjectFlow PR #74 exact head `4fbecb34bc570bb24c640b178d3605d87f230c30` had 0 workflow runs and 0 statuses (NOT PASS); recheck any subsequent docs-only head. Corrected full reference blob in GitHub tree to blob `ac0415b5ab687b7c980240412c558a6c538d3f79`. Nova exact head `2a326d84bbb7f031817403a9dbbcfbeb4590ad05` has two workflow failures: audit prototype/contract regressions and app CI clean/application + Postgres/browser initialization. No runs/status result is treated as PASS.
- Nova `TASKS.json` was untouched; no registry acceptance is claimed. No production or database mutation/deployment occurred. No credentials are in files/logs. Exact gate status is in `docs/visual-qa/evidence/issue-73/RECOVERY-20261123.md`.

## Next actions

1. Reconcile the proposed design reference with the existing React route; then repair the approved target to <=8% without hiding fields, cropping, masking or changing the reference.
2. Fix or unblock the actual Claude planner/Builder paths and full-suite failures, and provide a bounded test-only Docker IPAM environment without touching shared daemon networks.
3. Obtain actual passing exact-SHA CI on both drafts and all acceptance gates; keep PRs draft and do not claim DONE until those gates pass.
