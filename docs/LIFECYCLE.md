# Change Lifecycle (Productization Audit P0.3/P0.4)

The real, current lifecycle a Change goes through, as implemented in
`app/services/workflow_engine.py`. Written from actual code, verified
end-to-end by the golden deterministic fixture
(`tests/test_golden_e2e.py`).

## Stages and profiles

Three profiles (`PROFILES`/`PROFILE_STAGES` in `workflow_engine.py`):

| Profile | Intent | Required stages |
|---|---|---|
| `VIBE` | Minimal process, low-risk/internal/prototype | BUILD, VERIFY; DEPLOY/HUMAN_ACCEPTANCE only if triggered |
| `AGENTIC_STANDARD` (default) | The default autonomous engineering workflow | SPEC, BUILD, REVIEW, VERIFY; ARCHITECTURE/DESIGN/PLANNING/RELEASE optional; DEPLOY/HUMAN_ACCEPTANCE conditional |
| `CONTROLLED` | Stronger governance | SPEC, DESIGN, BUILD, REVIEW, VERIFY, RELEASE, HUMAN_ACCEPTANCE all REQUIRED |

Each stage owns zero or more gates (`GATES`/`GATES_BY_STAGE`); a stage is
`complete` only when every one of its gates evaluates true (or, for a
gate-less required stage, once at least one Task is attached — never a
fabricated signal).

## Gates (implementation / truth source / fallback)

| Gate | Stage | Truth source | Fallback |
|---|---|---|---|
| `SPEC_APPROVED` | SPEC | Approved `FEATURE_SPEC` WorkProduct exists | — |
| `ARCHITECTURE_READY` | ARCHITECTURE | Approved `ARCHITECTURE_ANALYSIS` WorkProduct, or `architecture_design_gate` (independent review) when wired | Bare WorkProduct-presence check |
| `DESIGN_READY` | DESIGN | Same pattern, `TECHNICAL_DESIGN`/`UI_UX_DESIGN` | Bare WorkProduct-presence check |
| `REVIEW_PASS` | REVIEW | `review_gate.review_pass()` (E9, per-Task, current-HEAD-pinned) when wired | Legacy per-workspace `builder.review_status` check |
| `SPEC_COMPLIANCE_PASS` | REVIEW | `SpecComplianceVerifier.verify()`, real Spec Layer registry (`specs/`) linkage — **never a default PASS**; a Change with zero spec-linked Tasks is UNMET, not vacuous | none — deliberately never falls back to vacuous PASS |
| `SECURITY_PASS` | REVIEW | `review_gate.security_pass()` (E9, applicability-aware) when wired | Legacy REVIEW_PASS-reuse |
| `TESTS_PASS` | VERIFY | Every active Task's `AUTOMATED_TESTS` checklist item (real `test_runs` pinned to exact HEAD) | — (excludes CANCELLED Tasks as of this audit — see below) |
| `TEST_DESIGN_READY` | VERIFY | `test_design_gate.test_design_ready()` when wired | Vacuously True (unwired, or nothing governs the Change at the Change-level spec trace-link) |
| `RELEASE_READY` | RELEASE | Every active Task is `READY_FOR_MAIN` or `DONE` | — (excludes CANCELLED as of this audit) |
| `DEPLOY_VERIFIED` | DEPLOY | `deploy_verified_gate.deploy_verified()` (Release's own PRODUCTION_VERIFIED truth) when wired | Legacy DEV-only `deployments` VERIFIED check |
| `HUMAN_ACCEPTANCE` | HUMAN_ACCEPTANCE | `human_acceptance_gate.gate_status()` (E11 `ProductAcceptanceService`, bound to the exact current production Release/artifact) when wired | Legacy approved `HUMAN_DECISION` WorkProduct |

`DEPLOY`'s own requirement is `REQUIRED_IF DEPLOYMENT_REQUESTED` (any
`deployments` row exists for a Task in the Change); `HUMAN_ACCEPTANCE`'s
is `REQUIRED_IF HUMAN_ACCEPTANCE_APPLICABLE` (`ProductAcceptanceService`
applicability: user-facing/mixed changes, or explicit policy).

**No aliases, no accidental default-PASS, and no double evaluation were
found** among these eleven gates on inspection — each is evaluated
exactly once per `evaluate_workflow()` call, from exactly one
`_gate_*` method. The one gate that looks unusual (`SECURITY_PASS`'s
docstring: "ProjectFlow has no distinct security-review data source
yet") is stale wording — as of E9, `SecurityReviewService` writes its
own `review_kind='SECURITY'` rows and `security_pass()` reads them
independently; the comment predates that and should be corrected
(cosmetic, not fixed in this pass).

## CANCELLED Tasks (P0.7/P0.10 finding, fixed in this audit)

A `CANCELLED` Task is the correct way to retire a Task whose own
worktree/branch identity can never be reused (see
PRODUCTIZATION_AUDIT.md's "workspace identity" finding). Before this
audit, a CANCELLED Task:

1. Could still be selected and relaunched by the scheduler
   (`AutonomousExecutionService.evaluate_task()`), reusing its own
   stale, already-abandoned workspace.
2. Permanently blocked `TESTS_PASS`/`RELEASE_READY` for the whole
   Change, since its checklist is deliberately empty and it is never
   `READY_FOR_MAIN`/`DONE`.

Both are fixed: `TaskDependencyService.readiness()` now returns a
dedicated `readiness="CANCELLED"` value (excluded from every
operational bucket — ready/waiting/blocked/complete), and
`_gate_tests_pass`/`_gate_release_ready` exclude CANCELLED Tasks from
their per-task loops. A cancelled Task now carries no weight, positive
or negative, in its Change's own completion calculus — matching how
`TaskDecisionService.evaluate()` already treated it.

## Proven Change-completion scenarios (P0.4)

Verified against real evaluator behavior (existing E3/E9/E11 test
suites plus this audit's own fixtures):

| Scenario | Verified behavior |
|---|---|
| VIBE, no Spec | `SPEC` stage not required; reaches COMPLETE from BUILD/VERIFY alone (`test_workflow_engine.py`) |
| User-facing AGENTIC_STANDARD | HUMAN_ACCEPTANCE becomes REQUIRED via `HUMAN_ACCEPTANCE_APPLICABLE`; blocked at WAITING_HUMAN until a real ACCEPTED ProductAcceptance exists (golden fixture) |
| Backend-only / NO_BEHAVIOR_CHANGE | SpecGate PASSes without requiring feature/requirement linkage |
| CONTROLLED | Every stage REQUIRED including DESIGN and HUMAN_ACCEPTANCE; proven end-to-end in `test_waiting_human_when_only_human_acceptance_remains` |
| Bug/incident-originated Change | Runs the identical lifecycle as any Change (`IncidentService.classify()` materializes a real `change_id`); proven in the P0.6 golden bug-closed-loop fixture |
| Stale Spec (design/test-design digest drift) | `check_design_staleness()`/`check_test_design_staleness()` correctly block `AUTO_READY` (`PLAN_DESIGN_STALE`/`PLAN_TEST_DESIGN_STALE`) until a Plan is re-pinned to the current baseline digest |
| Failed review | `submit-review BLOCKED` → Change status `BLOCKED`, never silently proceeds (`test_blocked_when_a_task_is_blocked`) |
| Deployed but not accepted | `DEPLOY_VERIFIED` met, `HUMAN_ACCEPTANCE` still unmet → `WAITING_HUMAN`, not COMPLETE |
| CANCELLED superseding Task | Now correctly excluded from the Change's own gates (this audit's fix) rather than blocking forever |
