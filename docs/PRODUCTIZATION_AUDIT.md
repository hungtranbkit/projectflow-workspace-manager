# Productization Audit P0 (2026-08)

**This is not a new engineering phase.** E1-E13 are complete; this
audit reconciles, simplifies, and proves the existing system before any
future public SaaS/App Builder layer, fixing only proven, reachable
contradictions. See `SOURCE_OF_TRUTH.md` (P0.1/P0.2) and
`LIFECYCLE.md` (P0.3/P0.4) for the domain map and gate inventory;
`TECHNICAL_DEBT.md` (P0.16) for the consolidated register;
`ARCHITECTURE.md` for the system-level view. This document covers the
rest: the golden fixture, real fixes made, and the readiness/security/
performance/App-Builder analysis.

## P0.5/P0.6 — Golden deterministic E2E fixture

`tests/test_golden_e2e.py` runs a disposable "inventory app" through
the REAL current lifecycle: Change → Requirement Analysis/Spec/
Architecture/Design/Test Design (real APPROVED WorkProducts) → real Plan
→ 2 parallel implementation Tasks (E13 wave, real concurrent launch,
real isolated git worktrees) → real CodeReview + Security review (E9,
fake LLM invoker) → real serialized Integration (E10) → real
build-once Release → real TEST deploy → real PRODUCTION deploy+verify
→ real Product Acceptance (E11) → Workflow reaches `COMPLETE` from
real, chained evidence. **Both tests pass.** A second test on the same
fixture introduces a real production defect, runs it through Incident →
reproduction → regression TestCase → resolution Change → fix → review →
release v2 → deploy → verify → close, and proves the ORIGINAL Change's
own workflow state stays `COMPLETE`, untouched by the incident's
separate Change.

Building this fixture is what surfaced every real bug fixed below —
each was found because the fixture insists on real evidence at every
step, never a shortcut.

## Real bugs found and fixed in this audit

### 1. CANCELLED Task could still be scheduled and could block Change completion forever (P0.7/P0.10)

**Root cause**: `TaskDependencyService.readiness()` only mapped
`status=="DONE"` to `readiness="COMPLETE"`; a `CANCELLED` Task has no
unmet dependencies of its own, so it fell through to
`readiness="READY"`. This had two real, reachable consequences:

1. `AutonomousExecutionService.evaluate_task()`/`list_auto_ready_tasks()`
   could still select and relaunch a cancelled Task, reusing its own
   stale, already-abandoned workspace (found while building the golden
   fixture's own wave-recovery step, after a sibling Task's worktree
   became unusable per finding #2 below).
2. `WorkflowService._gate_tests_pass()`/`_gate_release_ready()` loop
   over every Task in the Change; a CANCELLED Task's checklist is
   deliberately empty (`TaskDecisionService._checklist()`), so it could
   permanently block `TESTS_PASS`/`RELEASE_READY` with no evidence that
   could ever satisfy it.

**Fix** (`app/services/workflow_engine.py`,
`app/services/autonomous_execution_service.py`): `readiness()` now
returns a dedicated `readiness="CANCELLED"` value (excluded from every
operational bucket — ready/waiting/blocked/complete, matching how
`TaskDecisionService` already treated it); `evaluate_task()` excludes
CANCELLED Tasks explicitly; `_gate_tests_pass`/`_gate_release_ready`
exclude CANCELLED Tasks from their per-task loops. A cancelled Task now
carries no weight, positive or negative, anywhere in its Change's
completion calculus. Regression:
`tests/test_productization_audit.py::test_cancelled_task_never_reselected_by_scheduler_or_workflow_gates`,
plus exercised live in the golden fixture's own recovery path.

### 2. Workspace identity is permanent — "fresh worktree for the same Task" is not achievable (P0.7)

`agent_workspaces.branch`/`.worktree_path` are DB-level `UNIQUE`
columns computed 100% deterministically from `(repository, agent,
task-slug)`; `remove_task_worktree()` only ever sets `status='CLOSED'`,
never deletes the row (deliberate history-preservation). This means
E13.34's own literal wording — "Wave 2 recomputed from current truth,
fresh worktree for the SAME Task" — is not actually achievable for the
same Task identity once its worktree/branch has ever been created,
even after full git-level cleanup. **Not fixed** (a genuine redesign —
either deleting history rows or changing the deterministic naming
scheme — with no strong evidence it is needed): the real, working
recovery pattern, proven in both E13's own suite
(`test_execution_wave_2_uses_updated_base`) and this audit's golden
fixture, is a genuinely new Task carrying the retried intent forward,
with the original marked `CANCELLED`. Documented in `ARCHITECTURE.md`
and `TECHNICAL_DEBT.md` (IMPORTANT, not BLOCKER — it has a working
workaround).

### 3. E13's own named test gap: declared-safe scopes, actual overlap (P0.7)

Closed. `tests/test_productization_audit.py::test_actual_scope_overlap_despite_declared_safe_scopes`
proves `recheck_actual_scope()` correctly flags `PARALLEL_PREDICTION_MISS`
+ `ACTUAL_SCOPE_OVERLAP`/`ACTUAL_SCOPE_CONFLICT_RISK` when two Tasks
predicted `PARALLEL_SAFE` (disjoint declared scope) both actually touch
a shared file, and that integration never silently double-applies
either sibling's changes.

### 4. Second real instance of the E11/E12 connection-scoped insert-id race (P0.9)

`plans.(change_id,revision)` is UNIQUE; `plan_change()`'s
`MAX(revision)+1` read was a separate statement from its INSERT — the
same SELECT-then-INSERT race class already fixed once in E13's own
`wave_number`. Fixed with the identical bounded-retry-on-collision
pattern (5 attempts) in `app/services/planner_service.py`. Regression:
`tests/test_productization_audit.py::test_concurrent_plan_creation_never_collides_on_revision`
(4 concurrent threads, asserts revisions `[1,2,3,4]`, zero unhandled
errors).

A third instance exists (`releases._next_version()`,
`COUNT(*)+1`/`releases.(repository_id,version)` UNIQUE) but is
lower-frequency (needs two concurrent `create_release()` calls with no
explicit version for the same repository — release creation is
normally a single, human/RELEASE_MANAGER-gated action, not a hot
auto-triggered path like Plan-per-Task or Wave-per-parallel-launch).
Documented, not fixed, in `TECHNICAL_DEBT.md` (NICE_TO_HAVE).

### 5. Bounded retry for the shared real-provider structured-invocation layer (P0.8)

`PlannerAgentInvoker` (`app/services/planner_service.py`) is the ONE
shared subprocess boundary for every LLM role (Planner, Requirements
Analyst, Spec Author/Reviewer, Architecture/Design authors/reviewers,
Test Design, CodeReview, SecurityReview — one `planner_invoker`
instance, wired once in `app/main.py`). It previously raised
`PlannerAgentError` on the FIRST subprocess-level failure (non-zero
exit, timeout, or a CLI-flagged non-success envelope — the documented
E4-E6 `stop_reason=tool_use`/exit-1 flakiness class), with zero retry.

**Fix**: `invoke()` now retries once (bounded, `max_attempts=2`,
never infinite) on subprocess-level failures only — every attempt is
still a brand-new, stateless subprocess, so the "fresh context per
call" guarantee (E5's own "reviewer must not inherit hidden author
reasoning" invariant) is unaffected. Deliberately **never retried**: an
exit-0 response whose stdout doesn't parse as the envelope, or a
successful envelope whose `result` isn't a string — both are genuine
invalid-structured-response defects in the model's own output, not a
transient failure, and retrying them would mask a real prompt/schema
bug. Evidence (stderr tail, attempt number, `stop_reason`) is preserved
in the final raised error either way. Regression: three new tests in
`tests/test_productization_audit.py` proving the retry recovers a
transient failure, is bounded (never more than `max_attempts` calls),
and never fires on a genuine invalid-response case.

## P0.11 — WorkProducts / workspace_events / test_runs / verification_reports

Not the same truth stored three times — four genuinely distinct
layers, none redundant:

| Table | Role | Authoritative for |
|---|---|---|
| `work_products` | Durable, versioned **artifacts** (Spec/Design/Release manifest/Incident evidence) | "What was decided or produced" |
| `workspace_events` | Append-only **audit log** | "What happened, when" — never re-derived truth, pure history |
| `test_runs` | Raw **execution evidence**, pinned to an exact commit | "Did this command actually pass, at this exact HEAD" |
| `verification_reports` | The Builder's own **self-report** (`work_status`, `what_changed`, `how_to_verify`) | "What the Builder claims it did" — a claim, not proof |

`TaskDecisionService`/`SpecComplianceVerifier` read `test_runs` and
`review_runs`/`merge_records` as actual evidence; they never treat a
`verification_reports` row's own claims as sufficient on their own
(aside from snapshotting `spec_feature_id` linkage at submission time).
No consolidation needed — each layer answers a different question, and
merging them would lose that distinction.

## P0.12 — UI complexity (proposal only, not implemented)

Change Detail is 13 real tabs (`app/templates/_change_header.html`):
Overview, Spec, Architecture, Design, Tests, Plan, Tasks, Reviews,
Decisions, Evidence, Release, Deploy, Acceptance. Proposed future
**Simple Mode** grouping (Advanced Mode keeps all 13, unchanged):

| Simple Mode group | Absorbs |
|---|---|
| Overview | Overview |
| Product | Spec, Acceptance |
| Build | Architecture, Design, Plan, Tasks, Tests |
| Review | Reviews, Evidence |
| Deploy | Release, Deploy |
| History | Decisions |

Not implemented in this pass (proposal only, per P0.12's own scope).

## P0.13 — Public-product readiness scores (0-5)

Scored across 15 dimensions for three tracks: **A** open-source dev
tool, **B** hosted ProjectFlow service, **C** Vietnamese AI App
Builder.

| Dimension | A: OSS tool | B: Hosted service | C: App Builder |
|---|---|---|---|
| Core engineering lifecycle correctness | 4 | 4 | 3 |
| Multi-tenancy / data isolation | N/A (single operator) | 0 | 0 |
| AuthN/AuthZ | 0 (none exists) | 0 | 0 |
| Real-provider reliability (P0.8) | 3 | 3 | 2 |
| Parallel execution safety (E13) | 4 | 4 | 3 |
| DB integrity under concurrency (P0.9) | 4 | 3 | 3 |
| Performance at scale (P0.18) | 2 | 1 | 1 |
| Observability (`workspace_events`, evidence) | 3 | 2 | 2 |
| Onboarding / setup friction | 2 | 1 | 1 |
| Documentation (this audit's own docs) | 3 | 2 | 2 |
| Security posture (P0.17) | 3 (trusted single user) | 0 | 0 |
| Sandbox isolation of untrusted code | 2 | 0 | 0 |
| Billing / usage metering | N/A | 0 | 0 |
| UI complexity for a new user (P0.12) | 2 | 2 | 1 |
| Test coverage / regression discipline | 4 | 4 | 3 |

**Overall**: A (open-source, self-hosted, single trusted operator) is
the only track close to ready today. B and C both require real
multi-tenancy, auth, sandboxing, and performance work before any
public exposure — see P0.17 and TECHNICAL_DEBT.md.

## P0.14 — App Builder component gap analysis

| Component | Status |
|---|---|
| Account | MISSING |
| Organization | MISSING |
| Tenant | MISSING |
| Application (maps to Change/Repository) | PARTIAL |
| AppEnvironment (maps to Deployment) | PARTIAL |
| Template | MISSING |
| Runtime | SHOULD_REUSE_EXTERNAL_COMPONENT (a managed container/PaaS runtime, not reinvented) |
| Domain | MISSING |
| Subscription/Usage-Billing | MISSING — SHOULD_REUSE_EXTERNAL_COMPONENT (Stripe or equivalent) |
| ChatSession | MISSING (PlannerAgentInvoker is stateless-per-call by design — a chat-driven builder needs a genuinely new, stateful conversational layer) |
| App-Preview | MISSING |

No tables created in this pass, per P0.14's own scope.

## P0.15 — Golden stack recommendation (not implemented)

Recommend **A: FastAPI + Jinja/HTMX + Postgres** for a future
constrained App Builder — it is the closest continuation of
ProjectFlow's own current architecture (FastAPI + server-rendered
Jinja2, SQLite standing in for Postgres today), minimizing the number
of genuinely new architectural patterns the team would need to learn
and operate, and HTMX covers the interactivity a chat→app→deploy flow
needs without adopting a full SPA build pipeline. B (React) and C
(Next.js) both introduce a second language/build toolchain with no
evidence yet that the App Builder's own UI needs client-side
complexity beyond what HTMX provides.

## P0.16 — Technical debt register

See `TECHNICAL_DEBT.md`.

## P0.17 — Security productization audit

Evidence gathered directly from the codebase (not inferred):

| Risk area | Evidence | Classification |
|---|---|---|
| AuthN/AuthZ | No login/auth middleware found anywhere (`grep` for `login`/`authenticate`/`Authorization`/`@app.middleware` returns nothing) | MUST_FIX_BEFORE_PUBLIC_BETA (by design for today's self-hosted/localhost deployment — NOT_APPLICABLE for track A as currently scoped) |
| Multi-tenant data isolation | Single SQLite DB, no per-user/org row scoping anywhere in the schema | MUST_FIX_BEFORE_PUBLIC_BETA (tracks B/C) |
| CSRF protection | No CSRF token generation/validation found (`grep -rl csrf` returns nothing) | MUST_FIX_BEFORE_PUBLIC_BETA if ever exposed beyond localhost |
| Command execution (`shell=True`) | Real, confirmed: `gate_waiver_service.py:60`, `test_runner.py:24`, `app/main.py:4218` shell-execute PROJECT.yaml-defined commands | CAN_WAIT for track A (same trust boundary as running any repo's own build scripts); MUST_FIX_BEFORE_PUBLIC_BETA for B/C (a hosted service running arbitrary tenant-supplied PROJECT.yaml commands via `shell=True`, unsandboxed by default, is a real remote-code-execution vector) |
| Sandbox isolation | `SandboxManager` (Docker-based) exists but is opt-in; Builder git worktrees run directly on the host filesystem by default | MUST_FIX_BEFORE_PUBLIC_BETA (B/C) — sandboxing must become mandatory, not opt-in, once tenant-supplied code executes |
| SQL injection | All dynamic SQL found uses parameterized `?` values; dynamic column/SET-clause interpolation (`release_service.py`, `deployment_service.py`, `incident_service.py`, `app/main.py`) is always built from a fixed, code-controlled key allowlist, never raw user input | NOT_APPLICABLE — no injection vector found |
| XSS | Jinja2 autoescapes by default; only 6 `\|safe` usages found, all in `workspace_detail.html`/`task_detail.html` for server-generated `resume_form`/`block_form` HTML snippets (not confirmed free of user-controlled substrings in this pass) | CAN_WAIT — narrow surface, worth a dedicated follow-up read of the form-string generator, not re-audited line-by-line here |
| SSRF via deployment health checks | `DeploymentService`/`Deployer` issue real HTTP GETs to URLs sourced from `PROJECT.yaml` (`service.healthcheck.url`) | CAN_WAIT for track A (operator's own repo); MUST_FIX_BEFORE_PUBLIC_BETA for B/C (tenant-controlled URL reaching the hosting network) |
| Prompt injection via repo content | `PlannerContextBuilder`/CodeReview prompts include real repo/spec content the Task's own worktree controls | CAN_WAIT — bounded blast radius today (structured JSON-schema output, tool-less `--tools ""` invocation limits what a successful injection could actually do), but relevant if App Builder chat surfaces untrusted content more directly |
| Rate limiting / DoS on API routes | None found | CAN_WAIT for track A; MUST_FIX_BEFORE_PUBLIC_BETA for B/C |
| Secrets handling in logs/transcripts | Not verified in this pass — no dedicated redaction layer found, but also no confirmed leak; needs a dedicated follow-up | CAN_WAIT (flagged, not scored) |
| Dependency/supply-chain audit | Not performed in this pass (no lockfile/CVE scan run) | CAN_WAIT (flagged, not scored) |
| Path traversal (worktree/repo paths) | `WorktreeManager`/`git_workspace.py` validate paths against known repository roots on every access observed in this session's own investigation, but not re-audited exhaustively here | CAN_WAIT (flagged, not scored) |

No sandbox implementation was added in this pass, per P0.17's own
explicit scope limit.

## P0.18 — Performance/scale baseline

Measured with a real, disposable in-process fixture: 100 Changes × 5
Tasks each (500 Tasks total), seeded via the real API/service layer
(not raw SQL), using the same `TestClient` harness every other test in
this repo uses.

| Operation | Measured | Verdict |
|---|---|---|
| Seed 100 Changes × 5 Tasks | 3.15s | Acceptable (setup cost, not a hot path) |
| `GET /changes` (100 Changes listed) | **15,972ms** | **Serious, real bottleneck** |
| `GET /changes/{id}` (1 Change, 5 Tasks) | 445ms | Slow but tolerable for a single-record page |
| `evaluate_workflow()` (1 Change, 5 Tasks) | ~126ms/call | The dominant cost inside both pages above |
| `evaluate_pair()` (parallel safety) | ~14ms/pair | Fine at typical wave sizes (a 10-Task wave ≈ 45 pairs ≈ 630ms) |

**Root cause** (confirmed by design, not by profiler): `/changes`
calls `workflow_service.evaluate_workflow()` once per listed Change
(E11 already flagged this exact cost). Each `evaluate_workflow()` call
itself invokes `TaskDecisionService.evaluate()` multiple times per Task
(once inside `readiness()`, again inside `_gate_tests_pass`, again
inside `_gate_release_ready`, and potentially again inside
`_gate_spec_compliance_pass` for spec-linked Tasks) — with
`Database.execute()`'s own fresh-connection-per-call design, this
compounds into hundreds of separate SQLite connections for a single
page load at 100-Change scale.

**Not fixed in this pass.** A correct fix requires either (a)
memoizing `TaskDecisionService.evaluate()` results within one
`evaluate_workflow()` call — safe in principle (nothing mutates the DB
between reads inside a single synchronous call) but requires changing
`self.decision` from a fixed shared attribute to a per-call parameter
threaded through every `_gate_*` method, a mechanical but real
multi-method refactor — or (b) reusing a single DB connection per
request instead of opening a fresh one per `Database` call, which
changes a load-bearing assumption at least two other fixes in this
audit (P0.9's two SELECT-then-INSERT race fixes) were written against.
Both are legitimate fixes but not LOW-RISK ones; per this audit's own
mandate ("fix only serious LOW-RISK bottlenecks"), this is documented
as a **BLOCKER for hosted/public readiness** in `TECHNICAL_DEBT.md`
with real, reproducible numbers, recommended as a dedicated, properly
regression-tested follow-up rather than a rushed change here.

> **Correction (B1, 2026-09):** option (a) above was implemented by
> Track A1, after this P0 pass but before B1 started
> (`app/services/request_memo.py`, wired into `TaskDecisionService`/
> `WorkflowService.evaluate_workflow()`/`ChangeListSummaryService`).
> Re-measured with the same `scripts/benchmark_changes_list.py 100`
> at B1's start: **1,942.8ms**, not 15,972ms — the row above is
> historical (what P0 actually measured, kept for the record), not
> current. See `docs/TECHNICAL_DEBT.md`'s ALREADY_RESOLVED section.

## P0.20 — Regression summary

Full non-real-provider suite (`pytest tests/ -k "not real_"`): **all
green**, including the golden fixture and every new test this audit
added (concurrent Plan-revision race, LOW-risk DONE-evidence, NORMAL-
risk QA-required, actual-scope-overlap/prediction-miss, CANCELLED-task
scheduler/gate exclusion, bounded real-provider retry ×3, golden E2E
×2). Real-provider (`test_real_*`) qualification is intentionally kept
separate, unchanged by this audit — see each phase's own dedicated
real-Claude test for the stage it owns.

## P0.21 — Live-safe verification

See the FINAL REPORT delivered alongside this audit's commit for the
exact before/after live-service verification (health check, primary
page checks, no automatic production mutation, existing unrelated user
diff preserved byte-for-byte).
