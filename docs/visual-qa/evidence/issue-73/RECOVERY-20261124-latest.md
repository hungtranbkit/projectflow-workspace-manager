# Issue #73 — execution update (2026-11-24)

Status: **NOT DONE**. PRs remain drafts and issue #73 remains open. This report records the requested three NovaRetail measurement loops and latest exact-head checks; it does not infer acceptance from partial passes.

## A — ProjectFlow tests and failure classification: FAIL

- Full command `./scripts/test.sh`: **19 failed, 1551 passed, 1 skipped; exit 1; 2648.21 s**. Complete traceback: [`full-suite-final-attempt.log`](logs/full-suite-final-attempt.log).
- Provider-backed planner/review/spec/test-design failures returned `Planner exited 1 (attempt 2/2): subtype=unknown` (5 tests). One bounded separate Claude probe recorded HTTP 429 `usage_limit_reached`; this does not prove the five `unknown` responses were quota errors. Real Claude Builder E2E timed out after 420s and no target code was written.
- Sandbox/runtime group includes the reset, multi-repo soak, task sandbox view, verification, and workflow-anchor failures where sandbox status was `FAILED` instead of `RUNNING`. Prior container logs confirm Docker IPAM exhaustion on the shared runner. The later HTTP 409 / missing `backend_url` assertions are downstream symptoms of failed sandbox startup; avoid treating these as independent proof of visual branch regressions.
- Other full-suite failures include state invariant capacity cleanup, task-centric `next_action`/runtime identity, manual verification commit behavior, and workflow decision UX. These remain unexplained; do not attribute them all to provider quota or runner capacity.
- On 2026-11-24, the same eight previously failing UX/state cases passed both on the current branch (**8 passed, exit 0**) and in a temporary read-only `git archive` snapshot of base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3` (**8 passed, exit 0**). Exact base command and output: [`base-targeted-ux-state-20261124.log`](logs/base-targeted-ux-state-20261124.log). The temporary snapshot had a test-only `tests/__init__.py` marker to prevent a third-party installed `tests` package from shadowing `tests.conftest`; the base repository itself was unchanged. This rules out an isolated regression in those eight cases but does not explain their full-suite failures. Another isolated attempt had confirmed the package shadow (`tests.conftest` resolved to the Terminal MCP cache); adding repository `tests/__init__.py` fixed discovery and the formerly import-blocked closed-sandbox test passed. This branch-owned test-discovery fix does not change production behavior. No clean-base full-suite run was performed; other full-suite failures remain unclassified rather than labeled regressions or baseline defects.
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

`VISUAL_QA_REQUIRED: true` applies to UI tasks only; non-UI tasks remain unaffected. TaskDecisionService is still the source of truth. Copy/paste guide: [`docs/VISUAL_QA_HUONG_DAN.md`](../../../VISUAL_QA_HUONG_DAN.md). After adding `tests/__init__.py`, `./scripts/preflight.sh`, `./scripts/build.sh`, and `./scripts/smoke.sh` all passed; smoke ran 1 test. The eight selected UX/state tests passed as recorded above. The full-suite failure and visual CLI negative/fail-closed results remain as stated; this does not certify the full suite.

## E — exact-SHA PR/CI: NOT PASS

- ProjectFlow PR #74 exact head `3f63f694d9e4694f7940b53b005b90033325e967` (includes the `tests/__init__.py` import-shadow fix): exact workflow query returned **zero runs and zero check-runs**. Zero is not PASS. The local preflight/build/smoke and eight-test targeted evidence is recorded above.
- NovaRetail UI source/test head `c680931cda582e1effcf51726862404febef8b7e`: Application run [45262845323](https://github.com/hungtranbkit/novaretail-web/actions/runs/45262845323) and Audit run [45262845337](https://github.com/hungtranbkit/novaretail-web/actions/runs/45262845337) both completed failure. Subsequent docs-only exact head `a417ba7a701dbaab35192fd74ed431eabe85bb02`: push Application run [45266761113](https://github.com/hungtranbkit/novaretail-web/actions/runs/45266761113) and Audit run [45266761092](https://github.com/hungtranbkit/novaretail-web/actions/runs/45266761092) produced four completed exact-head check-runs, all failure. Audit parity and app graph findings repeat; browser/Postgres initialization again fails on shared-runner IPAM. The duplicate PR-event workflow [45266777828](https://github.com/hungtranbkit/novaretail-web/actions/runs/45266777828) remained queued at last query. These failures are separate from the preview, with details below.
- Prior exact-head Nova runs show app graph failure on four orphaned project files unchanged from PR base, and Browser/Postgres startup failure `Docker network create failed: all predefined address pools have been fully subnetted`. Runner reported machine `kimex-HP-EliteDesk-800-G4-DM-35W-Japan`; subsequent null container/network errors were cleanup fallout. This is shared runner IPAM capacity and is referred to the runner/repo owners in PR evidence. No network was removed or daemon config changed.

## F — safety: PASS

Dirty ProjectFlow `main`, production service/data, live order/API flow, and unrelated Docker networks were not changed. No production deployment or write, merge, force push, test bypass, or credential storage occurred. ProjectFlow untracked `initial/` and `after-strip/` evidence directories are preserved.

## Gate summary and next actions

| Gate | Latest status |
|---|---|
| A — planner/full suite | FAIL (19 failures; eight selected UX/state tests pass in isolation; provider/sandbox and other causes remain) |
| B — browser + visual gate | browser/tool execution PASS; 8% fidelity FAIL (16.723%) |
| C — real React preview | route and responsive behavior demonstrated; visual acceptance FAIL |
| D — opt-in and Vietnamese guide | targeted PASS |
| E — exact-SHA CI and PR checks | NOT PASS (ProjectFlow zero runs/checks; Nova eight check-runs failed) |
| F — safety | PASS |

Next: (1) the three permitted Nova repairs are exhausted at 16.723%; a future separately scoped attempt should focus on tabs/goods/totals without changing the design or gate; (2) ProjectFlow maintainers must resolve the unclassified suite/provider/Builder failures—five isolated tests passed singly and are not proven branch regressions; (3) runner/repository owners must restore shared Docker IPAM capacity and resolve pre-existing parity/build-graph inventory. Rollback only by reverting this task's own branch commits.
