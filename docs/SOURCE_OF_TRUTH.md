# Source of Truth Map (Productization Audit P0.1)

Generated from actual current code (not phase intentions) as part of the
P0 Productization Audit, 2026-08. For each domain: the authoritative
table(s)/service, and any DUPLICATE_TRUTH / LEGACY_TRUTH / DERIVED_TRUTH
/ COMPATIBILITY_FALLBACK flags found.

| Domain | Authoritative source | Flags |
|---|---|---|
| **Change** | `changes` table (`ChangeService`) | — |
| **Workflow** | `workflow_runs` + `WorkflowService.evaluate_workflow()` (derives `status`/`current_stage`/`unmet_gates` live from every other domain's own tables on every call; nothing is cached) | DERIVED_TRUTH by design — this is correct, not a duplicate: the workflow layer intentionally owns no independent evidence of its own. |
| **Task** | `tasks` table, `status` column (only `BACKLOG`/`ACTIVE`/`CANCELLED` are ever written — see below) | `TaskDecisionService.STATUSES`/`DISPLAY_STATUSES` |
| **Task completion decision** | `TaskDecisionService.evaluate()` | See P0.2 below — this is the single authoritative "what does DONE mean" function. |
| **Plan** | `plans` + `plan_items` (`PlannerService`) | `plans.(change_id,revision)` UNIQUE — race fixed in P0.9. |
| **AgentSession** | `agent_sessions` table (`AgentSessionManager`) | — |
| **Workspace** | `agent_workspaces` table (`WorktreeManager`) | `branch`/`worktree_path` are permanently UNIQUE even after `status='CLOSED'` — rows are never deleted (P0.7 finding, see PRODUCTIZATION_AUDIT.md). |
| **Review** | `review_runs` table, `review_kind` column distinguishes CODE vs SECURITY (`CodeReviewService`/`SecurityReviewService`, both writing the same table) | `TaskDecisionService.latest_review()` reads the most recent row **without** filtering by `review_kind` (imprecise, but bounded — see P0.2 below); the real production gates (`review_fix_orchestrator.review_pass()`/`security_pass()`, `WorkflowService._gate_review_pass`/`_gate_security_pass`) DO filter by `review_kind` correctly. |
| **SecurityReview** | Same `review_runs` table, `review_kind='SECURITY'`; applicability is a *separate*, deterministic computation in `SecurityApplicabilityService`, never persisted as its own row | DERIVED_TRUTH (applicability is recomputed, not stored) — correct, since applicability is a pure function of current task/worktree state. |
| **Finding** | `findings` table, linked to a `review_runs` row | — |
| **Verification** | `verification_reports` (Builder's own self-report, human/agent-authored) + `test_runs` (real command execution evidence) + `qa_runs`/`manual_verifications` (QA_VERIFIER role evidence) | Three distinct kinds of evidence, not the same truth stored 3 times — see P0.11 below. |
| **Integration** | `merge_records` table (`IntegrationService.integrate_task()`) | This is the ONLY place `merge_status='MERGED'` is ever written; `TaskDecisionService.all_merged` reads the same table. |
| **Release** | `releases` + `release_tasks` (`ReleaseService`) | `releases.(repository_id,version)` UNIQUE; auto-version path (`_next_version()`, `COUNT(*)+1`) has the same SELECT-then-INSERT race class as the two fixed in P0.9, but is lower-frequency (requires two concurrent `create_release()` calls with no explicit version for the same repo) — documented as NICE_TO_HAVE in TECHNICAL_DEBT.md, not fixed in this pass. |
| **Deployment** | `deployments` table (`DeploymentService`) | DEV/PRODUCTION environments share one table; `WorkflowService._gate_deploy_verified` prefers `ReleaseService`'s own PRODUCTION_VERIFIED truth when wired, falls back to a raw DEV-only `deployments` check when not — a real, working COMPATIBILITY_FALLBACK (E10.23), not a duplicate. |
| **ProductAcceptance** | `product_acceptances` + `product_acceptance_checklist_items` (`ProductAcceptanceService`) | `WorkflowService._gate_human_acceptance` falls back to a legacy approved `HUMAN_DECISION` WorkProduct when `human_acceptance_gate` is unwired — real COMPATIBILITY_FALLBACK, deliberately never a substitute once wired (E11.13: "never a default PASS merely because production is healthy"). |
| **Incident** | `incidents` table (`IncidentService`); resolution flows through the SAME Change/Task/Review/Release machinery as any other Change | Incident closure evidence (`work_products` kind capturing `verdict`/`resolved_release_id`) is its own durable record, distinct from the resolving Release's own status. |

## Task completion — legacy vs. E9/E10 (P0.2)

**Corrected finding** (this audit; the prior E13 final report was
imprecise on this exact point): `TaskDecisionService.evaluate()` is
genuinely the ONE authoritative completion decision. For a `LOW`-risk
Task (`RISK_GATES = {"LOW": ("REVIEW",), ...}`), real `review_runs` PASS
(E9's `CodeReviewService`) plus real `merge_records` MERGED (E10's
`IntegrationService`) is sufficient, unaided, to reach
`status="DONE"`/`stage="COMPLETE"` — proven by
`tests/test_productization_audit.py::test_task_decision_service_done_reflects_real_e9_e10_evidence_for_low_risk`.
A `NORMAL`/`HIGH`-risk Task additionally, deliberately requires QA
evidence (`requires_qa`) — a real, intentional gate, not a legacy
workaround or a duplicate-truth gap.

**CANCELLED is a genuine third status**, not a display alias of DONE or
BACKLOG (`tasks.STATUSES = ("BACKLOG","ACTIVE","CANCELLED")` — these are
the only three values ever written to the column; `DISPLAY_STATUSES`
adds `BLOCKED`/`READY_FOR_MAIN`/`DONE` as **computed** presentation
values, never persisted). Before this audit, `CANCELLED` was correctly
excluded by `TaskDecisionService.evaluate()`'s own first branch, but was
**not** propagated to `TaskDependencyService.readiness()` (which only
mapped `status=="DONE"` to `readiness="COMPLETE"`, leaving a CANCELLED
Task falling through to `readiness="READY"`). This let a cancelled Task
still be selected by the scheduler and still poison two Workflow gates
(`TESTS_PASS`, `RELEASE_READY`, which loop over every Task in the
Change). Fixed in this audit — see PRODUCTIZATION_AUDIT.md.
