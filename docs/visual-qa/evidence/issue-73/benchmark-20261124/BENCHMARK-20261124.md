# Issue #73 — offline design tool benchmark

Run date: 2026-11-24 (ICT). This is a tool benchmark and ProjectFlow dispatch verification, not approval of the POS proposal or completion of issue #73.

## Frozen inputs and safety

- Nova worktree source branch: `feature/nwr-visual-reference-fixture-001`; local source/evidence HEAD `c21bc9c5d08be4e65e577f3c2d0ae61612cbc295`. React source file hashes match the previously published POS capture. No Nova source file was edited in this benchmark.
- POS proposal image was frozen before implementation: SHA-256 `c263f6b9a8326d18111c583bf3450c446507261d1164456eb941850599d5e0cf`; the approved legacy reference remains `docs/design/web-journey/06-order.png`, SHA-256 `a6406a15efeb0af044c83a19de836c4f78d8a1c80912b3c459383052424fb174` and is still `REFERENCE_NOT_EQUIVALENT` to the V3 order screen.
- Existing real React route: `/app/store-minh-anh/pos`. Viewport 1586×992 DPR1. UIFix 1.2.6 commit `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53`, fixed threshold 8%, Playwright 1.60.0, Chromium 153.0.8010.12.
- The POS target is an **unapproved design proposal**, valid only as a tool-acceptance fixture. No owner approval is inferred. No production data/API write, upload, merge, deploy, or shared runner change occurred.
- Tool scratch remained about 462 MB (limit 2 GB). one-shot-ui package installation used `--ignore-scripts`; no global package or browser install. DCGen model/API scripts were not run.

## Tool and license audit

| Candidate | Pin / license | Local execution | Result |
|---|---|---|---|
| `one-shot-ui` | Repository commit `b3f90a72d241d971f47b45089a70b1c53d09c1c5`, package 0.12.0, MIT | `npm ci --include-workspace-root --ignore-scripts --no-audit --no-fund` and `npm run typecheck` passed. Source CLI `extract`, `compare`, `suggest-fixes` ran locally through `tsx`; image operations made no network calls. | Useful as advisory image/layout triage, but its pixel metric materially under-detected the controlled regression and its generic CSS suggestions did not consistently match measured DOM geometry. Do **not** integrate it as a gate or replace UIFix. |
| WebPAI/DCGen | Repository commit `22af69b238fb5aa967cf3f05985a25b52481105f`; no LICENSE file in the checked snapshot. The paper is CC BY 4.0, DOI [10.1145/3729364](https://doi.org/10.1145/3729364). | Official repository code was not executed: `utils.py` imports hosted model providers and the generation path is not safe for a no-secret/no-upload benchmark. The code's model/API pipeline was not invoked. `npm run build` for one-shot-ui exited 127 because Bun is absent; no global install was attempted. | A small independent port of the paper's published separation-line detector is included as `dcgen-separation-port.py`. At the paper's published thresholds it found no valid division lines in these four screenshots, so each remained one leaf. This algorithm-only port is not the upstream DCGen program and does not perform code generation. |

The paper describes splitting screenshots recursively along low-variance, high-contrast separator lines, then generating and assembling code with a multimodal model. This run benchmarks only the deterministic comparison and segmentation pieces; no MLLM was called.

## A/B benchmark and controlled repair

Same frozen reference and existing actual image:

| Case | UIFix 1.2.6 (authoritative 8% gate) | one-shot-ui diagnostic ratio | Outcome |
|---|---:|---:|---|
| Existing React POS actual, SHA `38abc47ba3c3e91a523d43a02946f196b8309e452c18ee5b228a36d6d867dc0b` | 91,006 / 1,573,312 = **5.784358%, PASS** | 65,298 pixels = **4.150353%** | POS proposal fixture only; not approved product equivalence. |
| Controlled real-route CSS regression: temporary browser-only `.nr-pos-payment { transform: translateY(24px) }`, screenshot SHA `38179b6649e450e6032c0f2afaa089bf8cfbd001f5a685a10b3bc572f1d5fe21` | 148,048 / 1,573,312 = **9.409958%, FAIL** | 86,165 pixels = **5.476663%** | one-shot ratio rose only 1.326 pp and is not a reliable substitute for the fixed gate. |
| Repair: removed the temporary browser override, recaptured the unchanged real React route | 91,006 / 1,573,312 = **5.784358%, PASS** | same original frozen image input | The restored actual SHA exactly matches the prior committed actual. No CSS/source change remained. |
| Legacy new-order route against `06-order.png` | Existing UIFix result **11.524542%, FAIL** | 87,619 pixels = **5.569080%** | Keep `REFERENCE_NOT_EQUIVALENT`; no threshold or image manipulation. |

The original positive/repaired actual, UIFix diff/overlay, viewport captures, and DOM metrics remain in the existing Nova PR #160 POS proposal evidence directory. Controlled-regression `actual.png`, `diff.png`, and `overlay.png` are retained in the task worktree beside this report and are not uploaded; their SHA-256 hashes are recorded in `metrics.json`.

## Three actionable diagnostics, checked against source/DOM

1. **V3 shell contract is a design decision.** one-shot's top-left tile had 11.046% local mismatch and accounted for 29.621% of its measured mismatch pixels. Actual DOM places `.nr-v3-rail` at x=0, y=64, width=236, height=928 and includes icons; the unapproved reference's rail is label-only and uses a different group heading. Decide the approved shell contract before spending CSS work here. Do not remove V3 navigation to improve the proposal score.
2. **Table has an extra in-table status/pagination row.** The current DataTable emits “Hiển thị 1 trên 1 mục · đã hết danh sách” inside the actual table; the reference places its local-session/count note outside the table. The output adds about 36–39 px to the table region. The producer is the shared DataTable in `packages/ui/src/primitives.tsx`; if the POS design owner confirms the proposed layout, adjust the fixture composition or presentation while retaining accessible count/status information. Do not edit the shared primitive in this task-owned scope.
3. **Table header and row start are vertically early.** Current DOM evidence places the table at about y=519 and the header/row at y=558.5/y=596.5; design extraction placed them at y=525 and y=566.5/y=605.5. Inspect `.nr-pos-cart__header`, `.nr-pos-quick`, and `.nr-table-region` in `SalesScreens.tsx` and scoped preview CSS after the target is approved; preserve search, quantity, and computed amount behavior.

The comparator also reported 67 reference layout nodes versus 74 implementation nodes, 66 matched nodes, and a 23-element reflow cascade. These are triage signals, not proof that seven controls should be removed. Its two largest suggested size corrections were not accepted without checking geometry: one-shot reported an 8 px height mismatch for a region whose actual saved DOM rectangle was close to the design reference. The regression output repeated the same suggestions rather than identifying the injected payment-card translation.

## ProjectFlow dispatch, gate, and failure reproduction

- Added `tests/test_visual_qa_execution.py`. It runs the real `TestRunner.start()` background stage dispatch and real `TaskDecisionService.builder_tests_status()` against a fake isolated DB/command boundary. UI opt-in dispatches `test` then `visual_qa`; a visual stage exit 1 produces gate `FAIL`; a backend prompt without the marker dispatches only `test` and remains PASS. It does not invoke a provider or write to a real project.
- Fresh tests: `pytest -p no:cacheprovider -q tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py` — **17 passed, exit 0**; `node --test tests/test_visual_qa_cli.mjs` — **4 passed, exit 0**; `CHROME_PATH=<task-local-wrapper> VISUAL_QA_PLAYWRIGHT_FROM=<ProjectFlow-worktree> node tools/visual_qa/smoke.mjs` — **exit 0**, exact-positive 0%, negative 100%, repaired 0%, missing reference/dimensions/server rejected.
- The saved full-suite trace remains **19 failed / 1,551 passed / 1 skipped**, exit 1. This run did not repeat the 44-minute full suite, and it does not reclassify those 19 failures.
- Reproduced the existing import-shadow failure against clean base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`: `tests/test_b4_github_webhook_ingestion.py` failed collection (exit 2) because an installed third-party `tests.conftest` shadowed this repository. On the PR branch, repository `tests/__init__.py` resolves imports locally and the same test file passed **16/16, exit 0**. This confirms that deterministic test-import fix; it does not explain the 19 full-suite failures.
- The project-local image-to-code probe reports Codex/Claude CLI binaries installed but `runtime_invocation=UNVERIFIED` because no provider login was supplied. No independent coding-agent request was issued.
- ProjectFlow PR #74 exact-SHA CI remains zero runs because its workflow is manually disabled; do not call this PASS. Existing owner issue #77 owns workflow/full-suite follow-up.

## Reproduction

From the Nova worktree, run the existing POS capture with the pinned UIFix/browser variables recorded in `pos-proposal-20261124/logs/verification-20261124.md`. The controlled regression was created by a temporary `page.addStyleTag()` in a scratch copy of `capture.mjs`, then repaired by rerunning the original capture script. The reference bytes and React source hashes did not change.

For one-shot-ui source benchmark in an isolated scratch clone at the pinned SHA:

```sh
node_modules/.bin/tsx packages/cli/src/index.ts extract "$REFERENCE" --no-ocr --json
node_modules/.bin/tsx packages/cli/src/index.ts compare "$REFERENCE" "$ACTUAL" --no-ocr --json --heatmap one-shot-heatmap.png --top 30
node_modules/.bin/tsx packages/cli/src/index.ts suggest-fixes "$REFERENCE" "$ACTUAL" --no-ocr --json --top 12
```

Run the DCGen paper-method port on the frozen PNGs with `python3 dcgen-separation-port.py <reference.png> <actual.png>`; it is diagnostic only. Never use its segmentation output as a pass/fail metric.

## A–F status

| Gate | Status | Evidence |
|---|---|---|
| A full ProjectFlow suite | **FAIL** | 19/1,551/1; unchanged full-suite failure. |
| B visual tool and fixed metric | **PARTIAL** | UIFix exact-positive/negative/fail-closed passed. POS fixture is 5.784358%; legacy screen is 11.524542% and non-equivalent. one-shot is advisory and not suitable as gate. |
| C approved real React pilot | **BLOCKED** | Existing React POS route and behavior were tested, but the target remains an unapproved proposal fixture. |
| D ProjectFlow UI-only opt-in and Vietnamese guide | **PASS for targeted contract/dispatch** | 17 pytest + 4 CLI + smoke pass; `docs/VISUAL_QA_HUONG_DAN.md` remains the guide. |
| E exact-head CI | **FAIL/BLOCKED** | Nova exact-head runs failed; ProjectFlow has zero runs because the workflow is manually disabled. |
| F safety / isolation | **PASS** | Fixture-only route, no backend writes, no production/dirty-main/shared-runner mutation. |

Rollback: revert only the new ProjectFlow test/evidence commit on PR #74; the CSS regression existed only in a scratch capture script and was removed before the repaired screenshot. The prior POS proposal and historical legacy order failure remain intact.
