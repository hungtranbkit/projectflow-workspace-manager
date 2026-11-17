# PROJECT_CONTEXT — ProjectFlow Workspace Manager

Living handoff for the next agent. Read `AGENTS.md` first (repo contract), then this file.
Code, tests and git state win over this note when they disagree. Fix the note when that happens.

## Snapshot (2026-11-17)
- `main` @ 8bcff05: Harness monitor bridge, progress/trajectory, monitor report and monitor service are deployed on Dell from a clean main checkout (`/home/dell/workspace/projectflow-harness-main-20261115`). See `docs/HARNESS_MONITOR_INTEGRATION.md`.
- Branch protection on `main` requires the status `local-ci/verify`, which a local runner posts (not GitHub Actions). The GitHub Actions workflow `CI` is `disabled_manually`. Keep it off.
- Local CI tiers: `scripts/ci-test.sh fast|remaining` (Harness/monitor scope only).
- `/home/dell/workspace/projectflow-workspace-manager` (the original checkout) is on a dirty `main` (`PROJECT.yaml` modified plus untracked .deb files). Never write there; use a separate clean clone or worktree.

## Recent handoff — issue #52 Meta-Monitor P0 (branch `agent/claude/meta-monitor-p0-52`, draft PR)
- Added `scripts/harness_meta_monitor.py`, `tests/test_harness_meta_monitor.py` (25 tests, in the `fast` tier) and `docs/HARNESS_META_MONITOR.md`.
- Design to preserve: a separate owner-only, append-only ledger `~/.local/state/projectflow-harness/meta-monitor.db` (triggers block UPDATE/DELETE). Missing evidence is UNKNOWN/null. Structured blocker codes (EXTERNAL_OWNER vs PRODUCT_DEFECT vs ORCHESTRATION). One open experiment per monitor, with a single-variable diff. KEEP/REVERT need ≥2 new observations that agree with the evidence. Rollback is proposal-only and refuses over a concurrent prompt edit. STABLE_MAINTENANCE needs 7 days of measured cycles, a guard run that passed within 24h, and no recent orchestration defect or unresolved serious defect. A verified regression gives REGRESSION_ACTIVE.
- Verified: `PYTHON=<pf venv> scripts/ci-test.sh fast` → exit 0 (58 tests). A manual mutation check broke 8 key guards one at a time, and each was caught by a failing test. The `remaining` tier was not run (no files in it changed).
- No schema change to the Harness DB. No deploy. No schedule, prompt, permission or Actions change.
- **Code DONE ≠ runtime acceptance.** The ledger is empty in production; nothing records cycles yet.
- Next: (1) the Meta-Monitor cycle calls `cycle-record` with its real readback; (2) decide who records defects and when `guard-run` runs (needs owner approval if a schedule is involved); (3) after ≥7 days of real cycles plus one judged experiment, evaluate runtime acceptance.
- Related: PR #51 (Monitor Loop V5 docs) is still open and is not included in this branch.
