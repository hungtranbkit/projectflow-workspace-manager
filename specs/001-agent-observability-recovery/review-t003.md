# T003 — Conflict review vs AGENTS.md, Spec Layer, TaskDecisionService, Harness

**Date:** 2026-11-20 · **Status:** DRAFT review notes, not an approval.

| # | Rule / contract | Potential conflict | Resolution in P0 slice |
| --- | --- | --- | --- |
| 1 | Constitution I / FR-001: no second scheduler or task ledger | Event store could become a second task DB | Store only holds `agent_obs_*` observation tables (events, dedupe, bindings, quarantine, counters). No task state, no timer, no worker. It never writes `harness_tasks`/`harness_events` or ProjectFlow tables. |
| 2 | AGENTS.md "If you need a new derived fact about a Task, add it to TaskDecisionService" | Delivery stage could compete with TaskDecisionService DONE/READY_FOR_MAIN | View reports *evidence milestones* only; READY_FOR_MAIN/DONE are deliberately **not** derived. Integrating into `TaskDecisionService` is left for review. |
| 3 | Spec Layer: behavior change needs approved FeatureSpec + SpecGate | No `specs/features/agent-observability-recovery.yaml` exists (T004 pending) | **Open blocker.** Code is additive/read-only and unlinked; no FeatureSpec YAML created, nothing marked approved, SpecGate not bypassed or self-approved. Merge must wait for T004 review. |
| 4 | SpecRegistry baseline digest covers YAML under `specs/` | New spec files could change digest | Only Markdown added under `specs/001-…`; SpecRegistry reads YAML only, so digest unchanged. |
| 5 | Harness: CENTRAL_ATTESTED ≠ RUNNING/DONE | Forwarded readbacks could be shown green | Provenance ceiling per source; attested data only yields DISPATCHED_UNVERIFIED or negative reasons. |
| 6 | Harness progress: % only from PASSED gates; TTL separate | Activity could inflate % | `derive_task_view` always returns `completion_percent=None`; only `attach_harness` copies Harness %. Test with 4 000-event burst. |
| 7 | Harness health: never repeat last green | Cached view could stay green | Stale evidence → execution UNKNOWN; stale/unhealthy snapshot → completion UNKNOWN + blocker. |
| 8 | ONE AGENT/BRANCH/WORKTREE, no git in app outside git_workspace.py | — | No git calls. Worktree exposed only as opaque `pf-workspace-{id}`. |
| 9 | B1 read isolation | New GET routes could leak cross-org data | Routes use `require_read_role("task","tid")`, same as `/api/tasks/{tid}/evidence`. |
| 10 | Constitution V privacy | Paths/prompts/transcripts | Adapter never selects them; normalizer allowlists fields, drops unknown keys, redacts secrets/paths/IPs; tests dump the whole SQLite file to prove absence. |
| 11 | No mutation from GET | API could persist events | GET routes use `view_from_raw()` (in-memory) over a `mode=ro` connection; test asserts table row counts unchanged and no `agent_obs_*` tables appear in the app DB. |

## Decisions still needed (not made here)
- Approve FeatureSpec IDs (T004) and link via SpecGate.
- Whether `ObservabilityEventStore` lives in the Harness SQLite (tested coexistence) or elsewhere; who runs ingestion (must be the existing reconcile service, not a new timer).
- Default stale (15 min) / stall (10 min) thresholds.
- Whether `delivery_stage` should be folded into TaskDecisionService output.
