# ORCH-STD-20261123 Tasks

**State:** Proposed DAG; T03–T06 are blocked pending approved FeatureSpec scope.

| Key | Executor step | Dependencies | Required evidence |
|---|---|---|---|
| T01 | Verify baseline and record safety facts | none | service/HTTP, worktree, DB, backup capability, disk, runners, spec inventory |
| T02 | Freeze contract, plan, task DAG, acceptance and runbook | T01 | five docs, pre-code SPEC/PLAN SHA256, DRAFT approval request |
| T03 | Add V2 prompt contract and all-entry SpecGate side-effect barrier | T02 + approved spec | negative zero-side-effect tests, V1 compatibility, valid V2 dispatch |
| T04 | Add derived goal closure and bounded recovery using current services | T03 + approved spec | EXITED≠DONE, concurrency/restart idempotency, 2-attempt cap and block states |
| T05 | Add feature-flagged autonomous reconciliation tick | T04 + approved spec | opt-in test, restart continuation, no duplicate process |
| T06 | Add user-visible Change/Task evidence flow | T03–T05 + approved spec | mobile UI, evidence separation, XSS and auth checks |
| T07 | Run focused regression/security/build and isolated staging canary | T03–T06 | commands/results, baseline comparison, real HTTP/session/report/QA and no duplicate |
| T08 | Deploy tested descendant to existing live service conditionally | T07 | DB backup/integrity, unit backup, local/public checks or documented untouched blocker |
| T09 | Publish evidence and commit scoped integration changes | T01–T08 | RESULT.md, EVIDENCE.json, hashes, commit, clean worktree |


## Owner-approved continuation tasks (2026-11-25)

The previous “blocked pending approved FeatureSpec” state is superseded by the explicit owner approvals recorded in `APPROVAL.md` and the approved canonical FeatureSpec.

| Key | Executor step | Dependencies | Required evidence |
|---|---|---|---|
| T10 | Invoke one leased recovery/DAG action from the existing CleanupWorker loop; add durable supervisor and per-Change fencing. | T04–T05 + owner continuation approval | Two-controller, takeover, restart, stale-owner side-effect tests; real callback recovery/blocker canary. |
| T11 | Add event-driven strong-model advisor through the existing PlannerAgentInvoker. | T10 + owner continuation approval | Sanitized strict schema, model-cache/request/receipt separation, Astra/high probe, Opus/high fallback receipt, 12-case eval, budget/cooldown/fault tests. |
| T12 | Repeat security/regression/staging gates and prepare scoped checkpoint/PR. | T07 + T10–T11 | Exact command logs, no new failures, review and release prerequisites; live remains unchanged until all release gates pass. |
