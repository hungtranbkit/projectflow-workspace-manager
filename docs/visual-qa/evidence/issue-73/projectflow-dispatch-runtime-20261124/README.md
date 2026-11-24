# ProjectFlow provider-dispatch runtime evidence

**Synthetic disposable acceptance only.** The POS proposal is unapproved and does not establish product/design approval. No production route, API write, or Nova source branch was changed.

- Reference SHA-256: `c263f6b9a8326d18111c583bf3450c446507261d1164456eb941850599d5e0cf` (same bytes in both captures).
- Route: `/app/store-minh-anh/pos`, `1586×992`, DPR 1; UIFix `1.2.6` pin `97c4b9d83684c7d607cb8c4b146e3f8aa5356e53`; Playwright 1.60 / Chromium 153; threshold unchanged at `0.08`.
- Project-local skill: `.claude/skills/image-to-code/SKILL.md`, SHA-256 `d737786ccd7512358c6d23bda7ddc2a1d04b7f3ece097242a55ae85932c1184e`. The slash skill was unavailable; the provider read this pinned file directly.

## Verified dispatch sequence

| Stage | Source SHA | `test_runs` | UIFix | TaskDecisionService |
|---|---|---|---|---|
| Seeded one-column regression | `8913bda32a647bfb399f13497e273cbab424307f` | `test PASS/0`, `visual_qa FAIL/1` | 419,773/1,573,312 = **26.680849% FAIL** | `builderTestsStatus=FAIL`, matching SHA |
| Single authenticated Builder repair | output commit `d308d481441eaea23e151e0126a92dfa4b596cd2` | editor via existing `/sessions` route; provider exit 0 | one scoped CSS grid edit; no second provider call | prompt DELIVERED; completion marker seen |
| Repaired stage | `d308d481441eaea23e151e0126a92dfa4b596cd2` | `test PASS/0`, `visual_qa PASS/0` | 91,006/1,573,312 = **5.784358% PASS** | `builderTestsStatus=PASS`, matching SHA |
| Stale check | `2e3a64eea93d56346e5fcb0987eef16a291c726f` | no new run | prior evidence rejected | `builderTestsStatus=NOT_RUN` |
| Backend opt-out | `2e3a64eea93d56346e5fcb0987eef16a291c726f` | only `test PASS/0`; no `visual_qa` row | not applicable | `builderTestsStatus=PASS` |

`negative/` and `repaired/` contain the captured reference, actual, diff, overlay, UIFix metrics, and report. Their manifest is `SHA256SUMS.json`. Exact test-run rows and sanitized stdout/stderr are `*-stage.json` / `*-stage.log`. The provider patch, redacted readable transcript, invocation timestamps, finish status, and scratch commits are included.

The actual editor boundary was ProjectFlow's existing Builder API and `AgentSessionManager`. The actual visual stage boundary was TestRunner subprocess execution using the pinned ProjectFlow visual CLI against loopback. `TaskDecisionService` remains the only result authority. No visual/editor side effect was added to the planner or test runner. Two prior CLI starts stopped at the folder-trust screen before a model call; one real provider invocation then completed.

## Replay recipe

In a task-owned disposable clone, configure `PROJECT.yaml` with the pinned local visual command and loopback preview route. Add the exact task marker `VISUAL_QA_REQUIRED: true`; run the React preview on loopback. Start the selected provider using the existing Builder action/session endpoint, and have it read the project-local pinned skill if slash discovery is unavailable. Then trigger the normal **Run tests** action, which calls `POST /api/workspaces/{id}/test`; inspect the resulting `test_runs` stage, source SHA, exit code, artifacts, and `TaskDecisionService.builder_tests_status`. Repeat after the provider's source edit. Advance the source SHA and verify the old result becomes `NOT_RUN`; create a separate backend task without the marker and verify it records only the normal test stage. Do not send provider credentials or screenshots to a remote service; do not treat an unapproved proposal as approved design evidence.

For reproducing the local execution in the originating task environment, the exact commands and wrapper path are preserved in the run logs. The temporary harness and SQLite database were task-owned, outside the product checkout.

## Remaining gates

This closes the **dispatch runtime milestone on the synthetic proposal only**. It does not close issue #73: the approved legacy order image remains `REFERENCE_NOT_EQUIVALENT` at 11.524542%; POS proposal evidence is unapproved; exact-head PR CI remains blocked/disabled; provider evidence is not approved-design evidence. ProjectFlow full suite remains previously verified on source SHA `cee006ca53eb36b7be29cf654fb3081844c4cce1` at 1,575 passed / 1 skipped, 3,137.38s, with 1,575 collected versus 1,576 outcomes unresolved.
