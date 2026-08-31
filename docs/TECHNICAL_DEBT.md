# Technical Debt Register (Productization Audit P0.16)

Every known limitation from E1-E13 plus this audit, classified
BLOCKER / IMPORTANT / NICE_TO_HAVE / OBSOLETE / ALREADY_RESOLVED.
"Blocker" means blocking public/hosted readiness (tracks B/C in
PRODUCTIZATION_AUDIT.md's P0.13 scoring) — not blocking today's
self-hosted single-operator use, unless stated otherwise.

## BLOCKER (public/hosted readiness)

None open as of B1 (2026-09). Every item this section originally listed
(written at P0, before B0/B1 existed) is now either fixed or moved to
ALREADY_RESOLVED below with real evidence — see that section rather
than assume this register is still accurate elsewhere without checking.

## IMPORTANT

- **Workspace identity is permanent** (`agent_workspaces.branch`/
  `.worktree_path` UNIQUE, rows never deleted). The one workaround —
  create a new Task rather than reuse the old one — is proven and
  documented (ARCHITECTURE.md), but it is a real constraint a future
  UI/automation layer must design around, not assume away.
- **`releases._next_version()` SELECT-then-INSERT race** (same class
  as the two fixed in P0.9), lower frequency (needs two concurrent
  `create_release()` calls with no explicit version for the same
  repository). Add the same bounded-retry pattern if ever observed in
  practice.
- **Real-provider structured-invocation layer still has a bounded,
  not unlimited, failure surface** — P0.8's retry (this audit) handles
  one class of transient failure; a persistently-unavailable provider
  still fails the whole invocation after 2 attempts, by design (never
  infinite retry). Acceptable, but worth monitoring in production.

## NICE_TO_HAVE

- **`review_runs` "most recent row" read in `TaskDecisionService.
  latest_review()`** has no `review_kind` filter (picks whichever of
  CODE/SECURITY was written last). Bounded — the real production-facing
  gates (`review_fix_orchestrator.review_pass()`/`security_pass()`,
  `WorkflowService._gate_review_pass`/`_gate_security_pass`) already
  filter correctly — but worth tightening for consistency.
- **`SECURITY_PASS` gate's docstring is stale** (`workflow_engine.py`)
  — says "ProjectFlow has no distinct security-review data source yet",
  which predates E9's own `SecurityReviewService`. Cosmetic.
- **UI complexity**: 13 Change Detail tabs, no Simple/Advanced mode
  split yet (P0.12 proposal only, not implemented).
- **`\|safe` usage in `workspace_detail.html`/`task_detail.html`**
  (`resume_form`/`block_form`) not re-audited line-by-line for
  user-controlled substrings in this pass — worth a dedicated,
  narrowly-scoped follow-up read.

## ALREADY_RESOLVED (B0/B1, 2026-08/09 — see docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md, docs/B1_HOSTED_SERVICE_READ_ISOLATION.md)

- **No AuthN/AuthZ** — B0.1 (real login/session/API-token identity)
  and B0.3 (`require_role()` swept across all 157 mutating routes) +
  B1.1 (`require_read_role()`/list-filtering swept across every
  GET route that can return another org's data).
- **No multi-tenant data isolation** — B0.2 (`organizations`/
  `organization_members`, `repositories.organization_id` as the one
  tenant-scoping anchor), enforced on writes by B0.3 and on reads by
  B1.1.
- **`/changes` list page: ~16s at 100 Changes** — fixed by Track A1
  (`app/services/request_memo.py`, wired into `TaskDecisionService`/
  `WorkflowService.evaluate_workflow()`/`ChangeListSummaryService`)
  after this line was originally written, before B1 started. Re-
  measured at B1's start with the same disposable-fixture benchmark
  (`scripts/benchmark_changes_list.py 100`): **1.9s**, not 16s — real,
  current evidence, not an assumption. No B1 code was needed for this.
- **`shell=True` execution of tenant-supplied PROJECT.yaml commands,
  unsandboxed by default** — B0.6 (`SandboxedCommandRunner`/
  `run_ephemeral()`, mandatory ephemeral-container isolation under
  `AUTH_MODE=required`, unchanged direct-host under `AUTH_MODE=none`).
- **No CSRF protection** — B0.4 (`require_csrf_unless_bearer`, folded
  into the same B0.3 sweep, plus B1.1's own `require_read_role()`
  deliberately NOT carrying it — GET is never CSRF-guarded).
- **No rate limiting** — B0.5 (slowapi on every named abuse-sensitive
  auth/org/token route).
- **SSRF surface via `PROJECT.yaml`'s `service.healthcheck.url`** —
  B1.2 (`app/services/ssrf_guard.py`), rejects loopback/private/
  link-local/metadata-address targets under `AUTH_MODE=required`;
  `AUTH_MODE=none`'s own real localhost DEV-target precedent stays
  unchanged (the guard never runs there).

## ALREADY_RESOLVED (this audit, P0)

- CANCELLED Task could be re-selected by the scheduler and could
  permanently block `TESTS_PASS`/`RELEASE_READY` — fixed
  (`readiness()`, `evaluate_task()`, `_gate_tests_pass`,
  `_gate_release_ready`).
- `plans.(change_id,revision)` concurrent-creation race — fixed
  (bounded retry-on-collision, same pattern as E13's own
  `wave_number` fix).
- E13's own named test gap (declared-safe scopes, actual overlap) —
  closed with a real fixture proving `PARALLEL_PREDICTION_MISS` +
  integration protection.
- Real-provider structured-invocation layer had zero retry on known
  transient subprocess-level failures — fixed with a bounded (never
  infinite), evidence-preserving retry, centrally, for every LLM role
  that shares `PlannerAgentInvoker`.

## ALREADY_RESOLVED (prior phases, for reference — not re-litigated)

- E11/E12: connection-scoped `last_insert_rowid()` mismatch in
  `product_acceptance_service.py`/`incident_service.py` (fixed prior
  to this audit).
- E13: `execution_waves.wave_number` SELECT-then-INSERT race (fixed
  prior to this audit; this audit's own `plans.revision` fix reuses
  the identical pattern).

## OBSOLETE

None found in this pass — every limitation traced back to either a
still-real gap or an already-fixed one; no dead/superseded concern
was carried forward from earlier phase reports without being
re-verified against current code.
