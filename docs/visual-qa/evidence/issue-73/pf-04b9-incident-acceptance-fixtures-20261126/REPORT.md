# PF-04b9 — incident/product-acceptance fixture residual

Date: 2026-11-26 (Asia/Bangkok, UTC+07:00)

## Source identity and scope

- PR #74 expected head at start: `f86550f6eda4fa2fc467205282ce4fff412d124a`; remote branch and `refs/pull/74/head` both returned this SHA.
- Remote tree: `814c6b2c0c746dec78912d843f6162440d43b200`.
- Existing task-owned worktree: branch `agent/codex/issue-73-engineering-fixture-20261125`, local HEAD `884a0f821325798686cd3a67a10c1f692e011d66`, same tree as remote; clean before task. Dirty main and other WIP were not touched.
- Exact tested source snapshot (parent plus scoped test edit): tree `13f358a455cff8e30dd5419489416e1262575f02`; `tests/test_product_acceptance.py` blob `c16c1b18b72ff5ef1527d2f049198bba68f48869`.
- Scope: one test fixture in `tests/test_product_acceptance.py`. No production files, provider invocation, real database, CI settings, deployment, or NovaRetail changes.
- Environment: Python 3.14.4, pytest 9.2.0, existing task venv; restricted `PATH=/usr/bin:/bin`.

## Finding and repair

The candidate run completed the incident path and most of the product acceptance flow, but the final workflow state remained `ACTIVE` with `unmet_gates=['TESTS_PASS']`. `make_release_repo()` seeds the committed test repo's `PROJECT.yaml` with `ci.required: [preflight, test]`; the acceptance scenario had seeded only the `test` row pinned to the actual submitted commit. The current `TaskDecisionService` correctly requires every contract-named stage. The fixture now seeds both `preflight` and `test`, pinned to that same committed `last_commit`. Gate logic and assertions are unchanged.

Base comparison was inconclusive. The first disposable archive collection hit installed `tests` package shadowing because base lacked `tests/__init__.py`. After adding an empty marker to that archive only, the test stopped earlier in `_reviewed_and_integrated_task()` because restricted PATH has no Claude CLI (`EXECUTION_FAILED`). It did not reach the final gate on base. This is not evidence that the gate change regressed.

## Test evidence

- Initial selected-three red: 2 PASS, 1 FAIL, 0 errors/skips, exit 1. The failing node was the final product acceptance status with `TESTS_PASS` unmet. See `selected-three.xml` and `selected-three.log`.
- After fixture correction, isolated acceptance node: 1/1 PASS, exit 0 (`candidate-positive.xml`).
- Full affected modules, unchanged selection: `tests/test_incidents.py tests/test_product_acceptance.py` — 45 passed, 0 failed, 0 errors, 0 skipped, exit 0. See `affected-modules.xml`, `.log`, `.meta`.
- All three historical node IDs in the fingerprint ledger map to PASS in the final affected-module JUnit. No live provider test was run or altered.
- Exact commands and timestamps are in `replay-commands.txt` and `*.meta`. This is targeted local evidence, not the full-suite gate or CI.

## Rollback and remaining gates

This source change is a test-only fixture correction. Rollback by reverting the scoped PF-04b9 source commit. No runtime resources remain. Full suite was not run. Gate A remains not PASS until the full suite is verified; E remains not PASS until required exact-head CI checks run and pass. B/C and visual approval gates are unchanged. Next residual groups remain productization audit, baseline waivers, authorized real-provider coverage, then full-suite qualification.
