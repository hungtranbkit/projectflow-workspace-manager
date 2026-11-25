# ORCH-STD-20261123 Plan

**State:** DRAFT — implementation authorization is BLOCKED by the spec boundary in SPEC.md. This plan is hashed before code changes; the hash must not be rewritten to excuse a failed gate.

| Gate | Work | Required proof / stop condition |
|---|---|---|
| T01 | Preflight designated integration worktree, live service/public URL, DB/backup/space, config secrecy, active sessions/runners, and approved specs. | Record exact nonsecret evidence. Stop if live baseline is not the stated strict release. |
| T02 | Create SPEC, PLAN, TASKS, ACCEPTANCE, RUNBOOK and this handoff; identify approved FeatureSpec trace and any DRAFT proposal; hash SPEC+PLAN. | Hashes recorded before implementation. Never change canonical specs without approval. |
| T03 | Selectively implement V2 contract validation and SpecGate pre-dispatch refusal, preserving QualityGate and V1 reads. | Requires approved scope for strict prompt validation; otherwise stop code work and record approval blocker. |
| T04 | Extend existing authoritative services for goal projection, single-owner recovery, bounded attempts/backoff/idempotency. | Requires approved recovery scope; no second status ledger or framework. |
| T05 | Integrate one feature-flagged reconciliation tick into existing autonomous service. | Requires approved scheduling scope; default OFF and no daemon/public mutation route. |
| T06 | Add mobile Change/Task Spec→Plan→Agent→Verify→Accept view with escaped content and honest evidence. | Requires approved UI scope and browser QA on authorized staging tab. |
| T07 | Focused tests, same-environment strict baseline comparison, build/preflight/smoke/compile/diff, isolated staging HTTP and real canary. | No missing Docker/Playwright/provider capability may count as PASS. One staging server, isolated disposable state. |
| T08 | Conditional live migration dry-run on DB copy, backup, unit/env preservation, reversible service switch, local/public checks and canary. | Any unmet precondition means leave live untouched and report exact blocker. |
| T09 | Record RESULT/EVIDENCE, exact hashes, commands, outcomes, commit scoped changes, leave clean worktree. | No claims beyond observed evidence; no forced merge or CI quota workaround. |

Execution ordering is T01 → T02 → spec approval → T03–T06 → T07 → T08 (conditional) → T09. T03–T06 cannot start until the owner approves the missing FeatureSpec scope.

