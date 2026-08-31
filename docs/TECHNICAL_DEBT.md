# Technical Debt Register (Productization Audit P0.16)

Every known limitation from E1-E13 plus this audit, classified
BLOCKER / IMPORTANT / NICE_TO_HAVE / OBSOLETE / ALREADY_RESOLVED.
"Blocker" means blocking public/hosted readiness (tracks B/C in
PRODUCTIZATION_AUDIT.md's P0.13 scoring) — not blocking today's
self-hosted single-operator use, unless stated otherwise.

## BLOCKER (public/hosted readiness)

- **No AuthN/AuthZ.** No login/session/authorization layer exists
  anywhere in the app. By design for today's localhost-only,
  single-operator deployment; a hard blocker for tracks B/C.
- **No multi-tenant data isolation.** One SQLite DB, no per-user/org
  row scoping anywhere in the schema.
- **`/changes` list page: ~16s at 100 Changes** (P0.18, measured).
  Root cause: `evaluate_workflow()` per Change × redundant
  `TaskDecisionService.evaluate()` calls per Task × fresh-SQLite-
  connection-per-call overhead. Fix requires either per-call
  memoization (mechanical refactor across `_gate_*` methods) or a
  connection-reuse-per-request architecture change (touches the
  fresh-connection semantics two of this audit's own race fixes rely
  on) — real work, deliberately not rushed into this audit pass.
- **`shell=True` execution of tenant-supplied PROJECT.yaml commands,
  unsandboxed by default.** Fine for a self-hosted operator running
  their own repo; a real RCE vector the moment a hosted service runs
  arbitrary tenant content this way. `SandboxManager` (Docker) exists
  but is opt-in, not mandatory.
- **No CSRF protection** on any POST route.
- **No rate limiting** on any API route.

## IMPORTANT

- **Workspace identity is permanent** (`agent_workspaces.branch`/
  `.worktree_path` UNIQUE, rows never deleted). The one workaround —
  create a new Task rather than reuse the old one — is proven and
  documented (ARCHITECTURE.md), but it is a real constraint a future
  UI/automation layer must design around, not assume away.
- **SSRF surface via `PROJECT.yaml`'s `service.healthcheck.url`** —
  real HTTP GETs to a tenant-controlled URL. Fine for track A; must be
  constrained (allowlist / network policy) before B/C.
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
