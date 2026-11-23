# Project Context — Issue #73 visual QA P0

Updated 2026-11-24. This is an open recovery, not DONE.

## Branches and safety

- ProjectFlow worktree: `/home/dell/workspace/.worktrees/projectflow-visual-qa-73`, branch `agent/codex/visual-qa-73`, base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`; local evidence update is being prepared from `b114a8b3a2dcf018e89380ba1b8a91351b6f7be7`. PR #74 remains draft. Dirty ProjectFlow `main` checkout was not touched.
- NovaRetail worktree: `/home/dell/workspace/.worktrees/novaretail-visual-reference-fixture-001`, branch `feature/nwr-visual-reference-fixture-001`; PR #160 draft remote head `76acf7a5a9910b2b0d9b83f24dbda07a3367147e` (tree `72d094e29cf6afb5ce252077e4a88602a3ff805b`, exact local match), base `4c3d76b3020da75ce3edc872b6119dda82ce3a2e`. No production writes or deploys.
- Visual QA remains explicit opt-in `VISUAL_QA_REQUIRED: true` for UI work; `TaskDecisionService` remains the source of gate decisions. Vietnamese guide: `docs/VISUAL_QA_HUONG_DAN.md`.

## Current evidence

- ProjectFlow preflight/build/smoke and deterministic visual CLI positive/negative/repair/missing-input gates have recorded passes. One provider-independent Planner/visual-contract run recorded 55 passed, exit 0. A later focused rerun collected 55, printed 29 dots, then exited 143 without summary; log and investigation note are under `docs/visual-qa/evidence/issue-73/logs/focused-planner-visual-latest*`. Cause of SIGTERM is unknown; no OOM evidence was found.
- Full ProjectFlow suite: 19 failed, 1551 passed, 1 skipped, exit 1, 2648.21 s. Provider 429 was confirmed by one bounded probe; other failures include UX/state/capacity/soak and Docker sandbox/IPAM cases, and are not all classified as quota. Real Builder E2E timed out at 420s. Do not skip or weaken tests.
- NovaRetail existing React preview route `/app/store-minh-anh/orders/new` was exercised in Playwright 1/1 at 1586x992, 430x932, 768x1024, 1366x768 and 1920x1080; diagnostics: no overflow, console/page errors, off-origin requests, failed requests or writes. UIFix against unchanged `06-order.png` SHA256 `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`, native 1586x992, fixed 8%: 21.515% → 18.511% → 16.605% (261,247/1,573,312 pixels, SSIM .8966) after preview-only shell/table polish at source `39ca1160687c27070889b4ff7d8f1ae19ffa7c0f`; still FAIL against 8%. Evidence report and screenshots are linked from PR #160.
- Previous Nova exact SHA `39293910c177dd2f5723609c584e490bbca2ff3b`: Audit run 45224044084 fails on unmapped `retail.issues`/`retail.store-settings`; Application CI 45224044123 fails build graph with four orphaned projects; parallel run 45224028944 failed browser and PostgreSQL container initialization. Current Nova exact head `76acf7a5a9910b2b0d9b83f24dbda07a3367147e`: Audit 45228285687 and App 45228298733 failed (same parity omissions and four orphaned build-graph projects). Run 45228285852 failed browser/Postgres initialization and app gate; duplicate Audit run 45228298864 failed. All four workflow runs and eight check-runs on the current exact SHA are completed failure. Local path hygiene itself passes.
- ProjectFlow PR #74 remote exact head `cabcd16cae27711ef31f519c10c10664698b4bbd` (tree matches local `52e8fa421ddf7bd19041cc742f08f7c187fe7e33`) has zero workflow runs in the exact-SHA Actions query. This is NOT PASS. Local merge commit is `0a0801353a889d39a53a1767fb6806a6698a72d6`.

## Next actions

1. Commit and sync current evidence/context to PR #74 through GitHub connector with exact tree verification; query CI on resulting SHA.
2. Continue Nova visual repair below fixed 8% without masking/cropping/reference edits; resolve distinct CI parity/build/container failures only with proper repo ownership and evidence.
3. Resolve ProjectFlow full-suite failures and unknown SIGTERM origin; retain PRs draft and issue open until all A–F gates and exact-SHA checks pass.
