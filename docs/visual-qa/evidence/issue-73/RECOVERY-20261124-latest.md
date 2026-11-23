# Issue #73 — execution update (2026-11-24)

Status: **NOT DONE**. PRs remain drafts and issue #73 remains open. This report records the requested three NovaRetail measurement loops and latest exact-head checks; it does not infer acceptance from partial passes.

## A — ProjectFlow tests and failure classification: FAIL

- Full command `./scripts/test.sh`: **19 failed, 1551 passed, 1 skipped; exit 1; 2648.21 s**. Complete traceback: [`full-suite-final-attempt.log`](logs/full-suite-final-attempt.log).
- Provider-backed planner/review/spec/test-design failures returned `Planner exited 1 (attempt 2/2): subtype=unknown` (5 tests). One bounded separate Claude probe recorded HTTP 429 `usage_limit_reached`; this does not prove the five `unknown` responses were quota errors. Real Claude Builder E2E timed out after 420s and no target code was written.
- Sandbox/runtime group includes the reset, multi-repo soak, task sandbox view, verification, and workflow-anchor failures where sandbox status was `FAILED` instead of `RUNNING`. Prior container logs confirm Docker IPAM exhaustion on the shared runner. The later HTTP 409 / missing `backend_url` assertions are downstream symptoms of failed sandbox startup; avoid treating these as independent proof of visual branch regressions.
- Other full-suite failures include state invariant capacity cleanup, task-centric `next_action`/runtime identity, manual verification commit behavior, and workflow decision UX. These remain unexplained; do not attribute them all to provider quota or runner capacity.
- On 2026-11-24, reran `./.venv/bin/python -m pytest tests/test_state_invariants.py::test_invariant_cleanup_eligible_still_counts_toward_capacity tests/test_task_centric_ux.py::test_task_detail_aggregates_child_state_and_never_duplicates_it -q`: **2 passed, exit 0**. This does not erase the earlier full-suite failures and indicates at least those two observed assertions are not consistently reproducible in isolation.
- The earlier 55-test provider-independent Planner + visual-contract run passed. Later 55-test process exited 143 after 29 dots with no pytest summary; SIGTERM origin remains unknown. `TaskDecisionService` remains the authoritative decision source; no assertions are weakened or skipped.

## B — Browser/visual tooling: execution PASS; acceptance FAIL

- ProjectFlow visual QA fixture contract previously passed exact, negative color, repaired, missing-reference, dimensions, and unavailable-server cases with fail-closed behavior. ProjectFlow preflight/build/smoke and Vietnamese docs have recorded passes.
- NovaRetail actual React preview, no second app: route `/app/store-minh-anh/orders/new`; connected `OrdersLive` unchanged; only fixture data/presentation was adjusted. No production writes, API/backend writes, or deployment.
- Original reference `docs/design/web-journey/06-order.png`, SHA-256 `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174`; reference pixels unchanged. UIFix 1.2.6 pin `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53`; 1586×992 DPR1; threshold fixed at 8%.
- Three measured source loops, no further loop permitted by this task: baseline **17.110%**; loop 1 `e10e4099728db047f0911e325776c3af11e64bad` **18.182%**; loop 2 `3da8100d11433bce147ae1fdcb80eae6b49e3763` **16.713%**; loop 3 `a46e236d32e0b1d7d3b7594b3a82d8ec0ec72e5f` **16.723%**, 263,098/1,573,312 pixels, SSIM .8976 diagnostic. Final fails 8%; it is 0.010 points worse than loop 2 and 0.387 points better than original. The remaining 8.723-point gap is not accepted.
- Regional mismatch for final loop: tabs **76.541%** (4.321 percentage points contribution); goods region **15.791%** (4.223 points); delivery/totals bottom **13.286%** (3.039 points). Regional crops are diagnostics; full unchanged-frame metric controls acceptance. See per-loop `regional-metrics.json`, `actual.png`, `diff.png`, `overlay.png`, `metrics.json`, `report.md` under NovaRetail PR #160 `prototypes/baoyu/NWR-VISUAL-REFERENCE-FIXTURE-001/react-pilot/loops/repair-01..03/`.
- NovaRetail `pnpm --dir apps/ui-preview lint`, `test`, and `build` passed (35/35 tests; build has existing >500 KB chunk warning). Playwright test passed 1/1 at 1586×992, 430×932, 768×1024, 1366×768, 1920×1080; diagnostics JSON has no overflow, page/console errors, failed/off-origin requests, or writes.

## C — Real NovaRetail React preview: demonstrated; fidelity acceptance FAIL

Route/data state are fixture-owned and concept-equivalent for the requested new-order view. Navigation remains reachable through the existing tab rail and overflow menu. Existing filter, cancel, reload, editable pricing, local state and preview/non-persistence behavior remain intact. Evidence on exact NovaRetail commit `c680931cda582e1effcf51726862404febef8b7e` (UI source SHA above): [pilot report](https://github.com/hungtranbkit/novaretail-web/blob/c680931cda582e1effcf51726862404febef8b7e/prototypes/baoyu/NWR-VISUAL-REFERENCE-FIXTURE-001/react-pilot/REACT-PILOT-20261123.md), [three loop artifacts](https://github.com/hungtranbkit/novaretail-web/tree/c680931cda582e1effcf51726862404febef8b7e/prototypes/baoyu/NWR-VISUAL-REFERENCE-FIXTURE-001/react-pilot/loops), [final responsive diagnostics](https://github.com/hungtranbkit/novaretail-web/tree/c680931cda582e1effcf51726862404febef8b7e/prototypes/baoyu/NWR-VISUAL-REFERENCE-FIXTURE-001/react-pilot/loops/repair-03/browser).

## D — Opt-in and Vietnamese guide: targeted PASS

`VISUAL_QA_REQUIRED: true` applies to UI tasks only; non-UI tasks remain unaffected. TaskDecisionService is still the source of truth. Copy/paste guide: [`docs/VISUAL_QA_HUONG_DAN.md`](../../../VISUAL_QA_HUONG_DAN.md). Preflight/build/smoke and visual CLI failure/negative controls have recorded passes; this does not certify the full suite.

## E — exact-SHA PR/CI: NOT PASS

- ProjectFlow PR #74 remote head `14327ef5ba9e2e3eb628feced8c6ea9a3f65e3fc`: exact workflow query returned **zero runs**. Zero runs is not PASS.
- NovaRetail PR #160 remote head `c680931cda582e1effcf51726862404febef8b7e`: Application run [45262845323](https://github.com/hungtranbkit/novaretail-web/actions/runs/45262845323) remained queued on last query; Audit run [45262845337](https://github.com/hungtranbkit/novaretail-web/actions/runs/45262845337) failed the prototype/parity check. Audit logs are on shared runner `hp-novaretail-ci`; current check failure is not yet resolved.
- Prior exact-head Nova runs show app graph failure on four orphaned project files unchanged from PR base, and Browser/Postgres startup failure `Docker network create failed: all predefined address pools have been fully subnetted`. Runner reported machine `kimex-HP-EliteDesk-800-G4-DM-35W-Japan`; subsequent null container/network errors were cleanup fallout. This is shared runner IPAM capacity and is referred to the runner/repo owners in PR evidence. No network was removed or daemon config changed.

## F — safety: PASS

Dirty ProjectFlow `main`, production service/data, live order/API flow, and unrelated Docker networks were not changed. No production deployment or write, merge, force push, test bypass, or credential storage occurred. ProjectFlow untracked `initial/` and `after-strip/` evidence directories are preserved.

## Gate summary and next actions

| Gate | Latest status |
|---|---|
| A — planner/full suite | FAIL (19 failures; two isolated reruns pass; other causes remain) |
| B — browser + visual gate | browser/tool execution PASS; 8% fidelity FAIL (16.723%) |
| C — real React preview | route and responsive behavior demonstrated; visual acceptance FAIL |
| D — opt-in and Vietnamese guide | targeted PASS |
| E — exact-SHA CI and PR checks | NOT PASS (ProjectFlow zero runs; Nova Audit fail, App queued) |
| F — safety | PASS |

Next: (1) keep PRs draft and issue open; a future separately scoped repair must address tabs/goods/totals regions without altering the design or gate; (2) fix ProjectFlow failures by their owning causes and establish exact-SHA CI; (3) runner owners must restore Docker IPAM capacity and repository owners resolve parity/orphan-project checks. Rollback only by reverting this task's own branch commits.
