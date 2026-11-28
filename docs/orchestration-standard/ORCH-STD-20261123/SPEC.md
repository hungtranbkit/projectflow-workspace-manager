# ORCH-STD-20261123 — End-to-End Running and Usable

**State:** OWNER_APPROVED — the human owner approved `FEAT-ORCHESTRATION-STANDARD` on 2026-11-23; implementation remains subject to its tests and acceptance gates. The original locked SPEC.md hash was `ae81e9b4f97bb4b3d8dd909e28f2b78cb8cc9f3887363fd4c7ba44502a8e5ef1`. Canonical FeatureSpec SHA256 changed from `473dec993cbfbec81ec37c862ea3801b290bea6740aa7158d4a6f9a0e615221b` (draft) to `fb99357e910a39267aaf774a4e674a10ca6fc5e4fa18e955c46a78f563b639d4` (approved); only the `status` value changed. The original PLAN hash remains `f6ff1542d436b4e0b250c9f53068dc861d3530558a366080e3f11a5b060bb20f`.

## Goal and ownership

One immutable Change is the parent goal and has one named human owner. Its ordered child Tasks are executor steps; each Task retains ProjectFlow's one-repository, one-branch, one-worktree rule. The Change's status is a live projection of existing TaskDecisionService, SpecGate, session, test, review, QA, integration, merge, and deployment evidence. No persisted `DONE`/accepted flag is introduced. Only independent user acceptance closes the goal.

## Governing approved scope and original approval

Reuse approved `FEAT-AGENT-EXECUTION` (REQ/AC/INV `AE-*`) for process start/exit, same-task resume, and acceptance gates; `FEAT-SPEC-LAYER` (`REQ-001..005`, `AC-001..007`, `INV-001..004`) for approved traceability; and `FEAT-AGENT-OBSERVABILITY` (`REQ-AO-*`, `AC-AO-*`, `INV-AO-*`) for read-only evidence display. Existing autonomous execution is opt-in and uses the existing `AutonomousExecutionService` and event records.

The original owner-approved scope is recorded in `APPROVAL.md` and the approved FeatureSpec. The earlier paragraph describing this as an unapproved proposal is retained in the audit history and superseded by that explicit owner decision.

## V2 task contract

Every new coding Task dispatched under this Change must have explicit links to an approved feature, at least one declared requirement, and at least one declared acceptance criterion, with SpecGate PASS. Its ChatGPT-authored prompt must contain these unique, nonempty sections: `OBJECTIVE`, `BASELINE`, `SCOPE`, `EXECUTION_PLAN`, `CONSTRAINTS`, `ACCEPTANCE_DOD`, `EVIDENCE`. Reject title-only, placeholder, filler, generic-wrapper, duplicate-header, missing-section, and incomplete legacy content. A refusal returns structured reasons before workspace, process, session, or queue side effects. Historical V1 Tasks remain readable; any migration is explicit and does not fabricate approval.

## Lifecycle and retry

The lifecycle is `DRAFT -> SPEC_APPROVED -> PLANNED -> READY -> DISPATCHED -> RUNNING_VERIFIED -> PROCESS_EXITED -> VERIFYING -> (RESUMING | BLOCKED | GATES_PENDING) -> ACCEPTED`. Process exit never means Task completion or acceptance. RUNNING requires fresh process/heartbeat plus progress evidence. Stale, unknown, offline, quota, or auth states become UNKNOWN/BLOCKED. Only existing review, risk-required runtime verification, integration, CI, merge, and deployment evidence can advance their respective gates.

One Change owner controls continuation; at most one active coding session may exist for the Change. Reuse the same Task, pinned contract, safe branch/worktree, and session identity only after confirmed exit, no competing process, available provider, and remaining retry budget. Retry limit is two, with durable attempt evidence and backoff. No retry occurs on uncertain liveness, offline/quota/auth, dirty/unsafe workspace, or ambiguous ownership. Exhaustion or unsafe conditions produce BLOCKED with a named owner and exact next action. Only the next eligible DAG Task may launch. Concurrent ticks and restart reconciliation must be idempotent.

## Owner-approved continuation — scheduled action and model policy (2026-11-25)

The owner extended the same P0 objective to require actual scheduled recovery actions, not status-only reconciliation, plus an event-driven strong-reasoning advisor for ambiguous incidents. This approval authorizes only the scoped orchestration capability in `REQ-OS-007..008` / `AC-OS-007..008`; existing auth, lease, dispatch, review, CI, merge, deployment, and provider-budget gates remain mandatory.

The existing `CleanupWorker` is the sole scheduled loop. It runs one leased `AutonomousExecutionService.scheduled_reconcile_tick()`, which performs report ingestion/recovery first and calls the existing DAG tick only if recovery found no action. `orchestration_leases` is coordination metadata, not a task/incident ledger. A random owner token and expiry must still match immediately before the dispatch boundary; recovery that loses ownership cannot continue. Per-Change dispatch has an additional lease in the same table. A stale owner, restart, unknown liveness, or unverified session never authorizes a retry.

Model advice uses the existing `PlannerAgentInvoker` only for ambiguous blocker fingerprints, with bounded sanitized evidence, tool-less structured output, a daily call limit/cooldown, exact model receipt, and deterministic fallback. The model proposes an enum action and evidence references; it never runs shell or changes task/review/release status. Preferred Astra/high remains unresolved through Codex CLI because its event stream did not attest the effective model. A bounded authorized Claude CLI probe verified `claude-opus-5-5`, requested `high`, and `firstParty` provider receipt; this is eligible as configured fallback, not a global CLI setting. User-approved criteria require 12-case evaluation and real scheduled canary before release.

## Threat model and invariants

Inputs include crafted Task text, XSS payloads, stale/deceptive heartbeats, duplicate/concurrent ticks, process exits, provider outages, and malicious or malformed evidence. Keep existing authentication/organization authorization, QualityGate behavior, trusted launcher registry, argv/shell safety, tenant scoping, and escaping. Do not expose prompts, secrets, transcripts, cwd, or credentials in summary UI/logs. No unauthenticated mutation endpoint, duplicate process, false RUNNING/DONE, retry loop, human-approval bypass for high risk, or gate collapse is permitted. `PROJECT.yaml` production deployment remains disabled.

## Rollback

Staging uses a disposable DB/state/root/worktree tree and non-production port. Live change is allowed only after all safety gates, online SQLite backup plus integrity/space checks, preservation of the original user unit and environment metadata, and a verified rollback command. Switch the existing user service once; never run two owners on 8765. On any failed live check, restore the saved unit/release immediately and prove active service plus local and public HTTP 200. Never edit the old strict checkout, its DB, or its unit before the conditional deployment gate.

