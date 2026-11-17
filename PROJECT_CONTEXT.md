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
- **Code DONE ≠ runtime acceptance.** The production ledger is empty.
- Follow-up (GAP_OBSERVABILITY): `ingest` reads the real `monitor-progress.json` (freshness ≤900 s plus HEALTHY) and the Harness DB read-only (`mode=ro`). It writes cycles with `provenance=HARNESS_SNAPSHOT`.
  - Redelivery is idempotent; a conflicting or out-of-order delivery is refused.
  - Deltas are computed only between two fresh cycles. A gate counts as `GATE_ACCEPTANCE` only if it has proof; a `DONE_VERIFIED` task counts as task output; a canary does not count.
  - Narrated `cycle-record` rows are labeled `OPERATOR_ATTESTED` and excluded from the metrics.
  - Ledger columns were migrated with ALTER (`provenance`, `source_ref`, `gate_passed`, `content_sha256`).
  - New file: `tests/test_harness_meta_ingest.py` (19 tests). There is also an opt-in `harness_monitor_service.py --meta-ingest` that is non-fatal.
- Verified: `scripts/ci-test.sh fast` → exit 0 (77 tests). Mutation check: 8 new guards, all caught. One real read-only smoke run wrote to a throwaway ledger (Harness DB sha unchanged); this is not acceptance.
- BLOCKED_OWNER: add `--meta-ingest` to the `ExecStart` of `projectflow-harness-reconcile-staging.service` after merge and deploy. Same timer, no new schedule.
- Next: activation, then ≥7 days of ingested cycles, then defect-record ownership, then (optionally) structured blocker codes in the gate policy, then judge experiments from the ledger only.
- Related: PR #51 (Monitor Loop V5 docs) is still open and is not included in this branch.
