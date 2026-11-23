# Project Context — Issue #73 visual QA P0

## Current state

- ProjectFlow task worktree: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`, local head `b2f97de317d6c029400e228ecfa9d3b7163600cf`; PR #74 remains draft. Dirty main checkout `/home/dell/workspace/projectflow-workspace-manager` was not touched.
- Visual QA stays opt-in via `VISUAL_QA_REQUIRED: true`, scoped to UI tasks. `TaskDecisionService` remains the gate authority. CLI and pinned UIFix 1.2.6 are in `tools/visual_qa/`; Vietnamese guide is `docs/VISUAL_QA_HUONG_DAN.md`.
- NovaRetail draft PR #160 is now at remote head `2a326d84bbb7f031817403a9dbbcfbeb4590ad05`, mergeable against main `4c3d76b3020da75ce3edc872b6119dda82ce3a2e`. Local Nova head `4d851e83bb2a6099e5acb1fffd64a1159559ffcd` has identical tree; connector created remote merge commit with main parent. Exact remote Nova CI currently has 0 runs/statuses.

## Verified results and blockers

- ProjectFlow quick preflight/build/smoke, visual CLI exact/negative-color/repaired/missing/dimension/server fail-closed smoke, and prior scope/planner classification tests passed. Provider-independent Planner (excluding the real Claude subprocess test) + visual-qa contract rerun: **55 passed, exit 0**; retained at `docs/visual-qa/evidence/issue-73/logs/provider-independent-contract-rerun.log`.
- Full `scripts/test.sh`: **19 failed, 1551 passed, 1 skipped, exit 1, 2648.21 s**. Four provider-backed flows report planner subprocess `subtype=unknown`; bounded real Claude CLI probe returned HTTP 429 `usage_limit_reached`. Real Claude Builder E2E timed out at 420 s without fixing `calc.py`. Five-file UX/runtime rerun: 37 passed / 8 failed, with retained Docker IPAM stderr `all predefined address pools have been fully subnetted`; do not attribute every full-suite failure to quota/IPAM. No 143 process termination is in the final log; its `143 warnings` are warning counts. Codex CLI disposable smoke could not execute even `pwd` (exit 159) in workspace-write sandbox; no files changed, no unsafe bypass (details in `logs/codex-disposable-builder-probe-20261123.md`).
- Existing NovaRetail React preview route passes Playwright 1/1 across five viewports; zero overflow, off-origin/failed requests, console/page errors or write requests. UI preview lint, 35/35 unit tests and build pass (chunk warning). Against unchanged `06-order.png` SHA `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`, fixed 8% at 1586x992, reported before repair 36.333%, after scoped CSS repair 34.372%: visual fidelity remains FAIL / `REFERENCE_NOT_EQUIVALENT`. Current evidence is accessible via PR #160 and detailed in `RECOVERY-20261123.md`.
- ProjectFlow remote PR #74 head was `82fc583c104345e515d3ff0d85fb1674cb9a0e8f`; local branch is ahead but must be synced after report update. Exact-SHA runs/statuses for ProjectFlow have not been verified after sync. Prior Nova head `949bb03...` had Application CI failure due runner Docker IPAM and a separate audit pass; those do not transfer to current head.
- Nova `TASKS.json` was untouched; no registry acceptance is claimed. No production or database mutation/deployment occurred. No credentials are in files/logs. Exact gate status is in `docs/visual-qa/evidence/issue-73/RECOVERY-20261123.md`.

## Next actions

1. Commit and sync current evidence/report to PR #74 using GitHub connector compare-and-swap; verify the reference image blob is byte-identical remotely.
2. Query Actions runs and combined statuses on the resulting exact PR #74 and #160 heads; record 0 as NOT PASS and any actual failure accurately.
3. Resolve the proposed-reference mismatch with the design owner, then reach the fixed 8% threshold and remove the provider/Builder/Docker-IPAM blockers before declaring completion.
