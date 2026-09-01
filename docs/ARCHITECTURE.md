# Architecture (Productization Audit P0.19)

Written from the actual current codebase, not historical phase
intentions. ProjectFlow Workspace Manager is a single-process FastAPI
app (`app/main.py::create_app(settings)`), backed by one SQLite
database (`app/db.py`, append-only `MIGRATIONS` list, currently V30),
serving both a JSON API and server-rendered Jinja2 UI from the same
process. There is no separate frontend build, no queue, no external
database — the whole system is one `systemd` service
(`workspace-manager.service`) on `127.0.0.1:8765`.

## Layering

```
app/main.py            route handlers only; wires every service via
                        create_app(settings); zero business logic
                        beyond request/response shaping
app/services/*.py       ~50 services, each owning exactly one domain's
                        real persistence + decision logic (see
                        SOURCE_OF_TRUTH.md)
app/db.py               Database class; one fresh sqlite3 connection
                        per Database.execute()/.all()/.one() call
app/launchers.py        AgentLauncher -- the one real subprocess/pty
                        boundary that actually starts a Builder/
                        Reviewer/etc. Claude or Codex process
app/templates/*.html    Jinja2 UI, server-rendered, no client build
specs/                  THIS repo's OWN real Spec Layer (SpecRegistry,
                        SpecGate) -- hardcoded specs_root, not
                        per-project/per-test overridable (see below)
```

## Core object graph (constructed once, in `create_app`)

`Database` → `ChangeService`/`WorkProductService`/`TaskDecisionService`
→ `WorkflowCatalogService`/`TaskDependencyService`/`WorkflowService` →
`AutonomousExecutionService` (the single Supervisor launch path,
`_start_builder_session`/`_launch`) → `ParallelSafetyService`/
`ExecutionWaveService` (E13, a pure scheduling *interpretation* of the
same Task DAG, never a second Plan) → `CodeReviewService`/
`SecurityReviewService` (share one `planner_invoker`) →
`ReviewFixOrchestratorService` (the kind-aware `review_pass()`/
`security_pass()` gates) → `IntegrationService` (E10, `git update-ref`
only, never touches the canonical checkout's working tree) →
`ReleaseService`/`DeploymentService` → `ProductAcceptanceService` (E11)
→ `IncidentService` (E12, reuses the same Change/Task/Review/Release
machinery for resolution, never a parallel state machine).

## Real-provider boundary

Every LLM-backed service (Planner, Spec/Architecture/Design authors,
CodeReview, SecurityReview) is invoked through one shared structured-
invocation layer (`claude -p --max-turns 1 --tools ""` /
`stop_reason=tool_use` parsing) — see PRODUCTIZATION_AUDIT.md's P0.8
section for its known flakiness and the decision not to add central
retry logic in this pass.

## Spec Layer isolation risk (operational note, not a bug)

`app/main.py` hardcodes `specs_root = Path(__file__).resolve().parent.parent
/ "specs"` — i.e. THIS repo's own real `specs/` directory, for every
environment including the test suite (`tests/conftest.py`'s `Settings`
never overrides it). This is intentional (the Spec Layer governs
ProjectFlow's own development), but means any ad-hoc script or demo
that materializes/mutates spec features must be run against an
isolated `specs_root`, or it risks corrupting this repo's own real spec
tree — always verify `git status specs/` before committing after any
such script.

## Isolation / concurrency model

- Each `agent_workspaces` row is a real `git worktree` with a unique,
  deterministically-named branch (`repo+agent+task-slug`) — real
  filesystem/git isolation per Builder, not a simulated sandbox.
- E13's `ExecutionWaveService` reserves Tasks atomically via a
  `task_reservations` primary-key uniqueness constraint before ever
  launching a subprocess, preventing double-launch races under real
  concurrent scheduling.
- `IntegrationService.integrate_task()` serializes all merges into the
  canonical branch via `git update-ref`; a worktree's `base_commit` is
  pinned at creation and immutable, so a second sibling in a wave is
  honestly blocked (`INTEGRATION_CONFLICT_AFTER_SIBLING`) once an
  earlier sibling integrates first — even with fully disjoint files.
  This is real, correct, conservative behavior (proven in both E13's
  own suite and this audit's golden fixture), not a bug.

## Known structural limitation: workspace identity is permanent

`agent_workspaces.branch`/`.worktree_path` are DB-level `UNIQUE`
columns; `remove_task_worktree()` only ever sets `status='CLOSED'`, it
never deletes the row (deliberate history-preservation). Because the
branch/path name is computed 100% deterministically from
`(repository, agent, task-slug)`, the exact same name can never be
reused for that same Task+repo+agent triple again, even after full
git-level cleanup. The only working recovery pattern (proven in both
E13's own suite and this audit's golden fixture) is a genuinely new
Task carrying the retried intent forward, with the original Task marked
`CANCELLED` — never "a fresh worktree for the same Task."

This is workspace *slot* identity — distinct from *repository*
identity, which used to be the filesystem path alone (a renamed/moved
repo directory re-registered as an orphaned duplicate row). B7.1
(`docs/B7_WORKSPACE_REPOSITORY_IDENTITY.md`) fixed that: `repositories.
git_fingerprint` is a real, local, git-derived identity (SHA-256 of the
sorted root-commit SHAs, `GitWorkspaceService.repo_fingerprint()`) that
survives a rename/move/re-clone/remote change, and `register()` rebinds
the existing row rather than duplicating it whenever the evidence for
that is deterministic (fingerprint match + the old path confirmed
missing). The workspace-slot limitation above remains unchanged and
unfixed by B7 — re-examined again this phase, still no new evidence
that today's "start a new Task" recovery pattern is actually
insufficient in practice.
