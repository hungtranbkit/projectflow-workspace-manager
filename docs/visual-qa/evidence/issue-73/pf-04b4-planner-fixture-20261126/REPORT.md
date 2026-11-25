# PF-04b4 — Planner Service deterministic fixture seam

## Scope and identity

Only `tests/test_planner_service.py` changes behavior. No production modules, live-provider invocation, credential, CI setting, main/WIP, Nova, or shared runner was touched. PR #74 was verified at remote head `5d1f2e0b982dcf4468a1939601040ceb3746b114`, tree `517f1255eba2810edd7a112ba3ff71b47e220cec`; expected base `ac288928ab559799e6a6f34ff9be1de5fdfa5af3`. Local branch `agent/codex/issue-73-engineering-fixture-20261125` was clean at start, local commit `e1a5cbab36bb196168c649841f19ff87854c6f27` had the same tree as published PR head. Tested Python 3.14.4 / pytest 9.2.0 with `PATH=/usr/bin:/bin`.

## Reproduction and cause

On candidate, the three requested nodes failed (exit 1): valid output, plan revision numbering, and usage-limit sanitization all surfaced `claude CLI not found in PATH` because deterministic helpers replaced `invoker.runner` only. `PlannerAgentInvoker.invoke()` resolves `which('claude')` before invoking the runner. On base, the two shared nodes (valid output and revision numbering) also failed. The usage-limit node is absent from the base SHA, so it could not be run there; base malformed-envelope negative was used as a separate available control. Logs/XML record both runs.

## Scoped repair and verification

A test-local `set_fake_runner()` now configures both resolver and runner. All deterministic fake paths in this module use it, including malformed, provider-limit, and retry-sequence cases. Added an unavailable-CLI regression proving the real resolver failure stays fail-closed: runner not called, no plan row. Existing live-provider test and its skip condition are unchanged.

- `PATH=/usr/bin:/bin $VENV/bin/python -m pytest --collect-only -q tests/test_planner_service.py` — exit 0, 43 collected.
- `PATH=/usr/bin:/bin $VENV/bin/python -m pytest -q -ra tests/test_planner_service.py --junitxml=<planner-module.xml>` — exit 0, 42 passed / 1 skipped / 0 failed / 0 errors. Skip reason: real Claude CLI unavailable; it was not counted as pass or deselected. The 30 historical failed node IDs map exactly to PASS; the historical provider-unavailable skip maps to SKIP. See `historical-planner-resolution.tsv`.
- `PATH=/usr/bin:/bin $VENV/bin/python -m pytest -q -ra tests/test_release_pipeline.py tests/test_visual_qa_contract.py tests/test_visual_qa_execution.py --junitxml=<scoped-regressions.xml>` — exit 0, 46 passed, no skip/failure/error.
- `git diff --check` — exit 0.

Test timing (ICT, 2026-11-26): candidate red log 04:26:45; base red 04:27:34; planner module complete 04:29:49; collection 04:30:04; scoped regressions complete 04:31:29. Logs have stdout/stderr and pytest summary; command exit is appended to each run log.

## Limits and next step

This is a deterministic test-fixture milestone only. No full suite and no real provider invocation. Full-suite Gate A remains not PASS; exact-SHA CI Gate E remains not PASS unless separately verified. Historical failures outside these exact 30 nodes are not claimed fixed. Rollback by reverting only the PF-04b4 commit on the PR branch.
