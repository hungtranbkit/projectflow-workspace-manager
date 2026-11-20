# Feature Specification: Agent Observability & Recovery

**Feature:** 001-agent-observability-recovery  
**Status:** DRAFT for review  
**Created:** 2026-11-20  
**Inspired by:** CrommaCode's observable CLI/tool calls, live file history, and per-agent lanes.  
**Product:** ProjectFlow Workspace Manager + existing Harness/Terminal MCP.

## Problem and desired outcome
The operator often sees a task described as running, while the underlying session may be idle, disconnected, awaiting input, silently stopped, or lacking new evidence. A visually active terminal is not proof of task progress. Provide a mobile-first read-only explanation of what each assigned agent actually did, which gate is passed, and what currently blocks forward progress, without replacing ProjectFlow's execution and acceptance authority.

## User scenarios and independently testable outcomes

### US1 — Verify real activity (P0)
As an operator, I can open a project and see the node, task, agent, session identity, last verified evidence and freshness. I can distinguish QUEUED, DISPATCHED_UNVERIFIED, RUNNING_VERIFIED, WAITING_FOR_INPUT, STALLED, FAILED, UNKNOWN and COMPLETED_WITH_PROOF. A newly issued prompt without process/tool evidence does not count as RUNNING_VERIFIED.
**Acceptance:** Simulate one live CLI event, one stale heartbeat, one missing central receipt, one user-input wait and one node disconnect; the UI distinguishes all five with a time and provenance reference.

### US2 — See a reliable causal timeline (P0)
As an operator, I can inspect an ordered stream of tool actions for main agents and subagents, including read/edit/command/test and Git changes where available; duplicate/replayed hook messages appear once and each item maps to the correct task/worktree/session.
**Acceptance:** Replay an out-of-order fixture twice; one normalized event appears for each unique event ID, and unknown identities are quarantined rather than assigned to a different task.

### US3 — Separate actual completion from activity (P0)
As an operator, I can see two independent progress indicators: acceptance-gate completion and time consumed against a child task's immutable TTL. A command success or passing technical canary is not proof of real user acceptance.
**Acceptance:** Changing event volume alone never changes the verified completion percentage; an expired child remains BLOCKED/CLEANUP_PENDING until independent cleanup proof.

### US4 — Take a safe next action (P1)
As an operator, I can see actionable blocker and owner-specific recovery suggestions. Unsafe/unowned or ambiguous sessions remain read-only; authorized retries are idempotent and do not duplicate processes or task IDs.
**Acceptance:** Two identical recovery requests cause no second live session; loss of owner proof prevents action and records reason.

### US5 — View the fleet on phone (P1)
As an operator, I can use a responsive dashboard showing Project → Task → Agent → Timeline, with summary and detailed evidence, without long horizontal scrolling. Operator/private and any separately reviewed public read-only presentation expose different fields.
**Acceptance:** At mobile widths 375 px and 428 px, core task status, blocker, last evidence, and two progress meters are usable without horizontal scrolling.

## Functional requirements
- FR-001: Preserve existing Task/Workspace/Agent identity and task acceptance gates. Do not create a competing task ledger or scheduler.
- FR-002: Collect sourced events via opt-in versioned CLI/Terminal MCP adapters with timestamp, node, project, task, session, agent/subagent, event kind, correlation key, provenance and redacted evidence pointer.
- FR-003: Deduplicate by stable identity/idempotency key; handle delayed, out-of-order and duplicate events without rewriting accepted history.
- FR-004: Model event provenance explicitly and distinguish submitted/dispatched from verified running, meaningful new output, and independently accepted result.
- FR-005: Surface UNKNOWN for unreachable nodes or unsupported integrations; do not reuse stale green health as current evidence.
- FR-006: Reuse existing Harness progress gate definitions and immutable TTL; never infer completion from calls, edits, duration or tokens.
- FR-007: Detect no-output-growth stalls separately from a live PID/heartbeat; make thresholds configurable and show why a task was flagged.
- FR-008: Preserve existing per-workspace single live process and isolated branch/worktree rules.
- FR-009: Show read-only Git diff/file-touch summaries without enabling unsafe branch changes or leaking internal paths to public views.
- FR-010: Provide a mobile-first read-only project/task/agent drill-down. Public summary exposure requires a separately reviewed allowlist and no control API.
- FR-011: Every CLI settings modification needs opt-in, a reversible backup and an audit receipt. Unsupported versions degrade to read-only observation.
- FR-012: P1 recovery must enforce ownership, bounded retries, durable deduplication, explicit approvals where needed, and no automatic merge/deploy/destructive cleanup.
- FR-013: Persist a bounded, queryable audit trail with evidence timestamp and freshness while preventing prompt/secret/raw-terminal leakage.

## Non-goals
- Recreating CrommaCode's desktop terminal or copying proprietary code/assets.
- Creating a second cross-node scheduler or replacing Terminal MCP.
- Auto-approving specs, bypassing SpecGate, changing main, auto-deploying production or terminating unknown sessions.
- Gamification, token leaderboards, arbitrary percentage estimates.
- Public internet deployment without an approved security boundary.

## Data and state contracts
Event minimum: schema_version, event_id, occurred_at, observed_at, source, node_id, project_id, task_id, session_id, agent_id, parent_agent_id?, kind, provenance_level, sanitized_evidence_ref?, dedupe_key.
Keep raw/private audit data distinct from sanitized UI read models. No missing identity should be silently inferred from a similar filename or terminal title.

Three separate dimensions:
1. Dispatch: QUEUED / DISPATCHED_UNVERIFIED / ACKNOWLEDGED.
2. Execution: RUNNING_VERIFIED / WAITING_FOR_INPUT / STALLED / EXITED / FAILED / UNKNOWN.
3. Delivery gate: CODING / TESTING / REVIEW / READY_FOR_MAIN / MERGED / DEPLOYED / ACCEPTED.
Never collapse these into one falsely optimistic state.

## Success criteria and acceptance metrics (proposed)
- SC-001: 100% of synthetic duplicate and reordered event fixtures are either deduplicated or explicitly quarantined.
- SC-002: 0 duplicate live agent launches in concurrent same-task start/recovery integration tests.
- SC-003: 100% of disconnected/stale-source fixtures show UNKNOWN/STALE, not RUNNING_VERIFIED or ACCEPTED.
- SC-004: Verified completion percentage remains identical under 1→10,000 activity-only events.
- SC-005: On a tested reachable node, 95% of accepted tool events appear on the operator view within 10 seconds (measured, not assumed).
- SC-006: Mobile layouts at 375 px and 428 px pass visual/readability smoke tests.
- SC-007: Public summary contract (only if approved for rollout) has zero secret/raw prompt/path/terminal-output fields in negative fixtures.

## Open decisions for clarify / review
- Final allowed adapter mechanism and installed CLI version matrix.
- Data retention/volume and exact source-of-truth event store owner.
- Whether live feed uses SSE or periodic polling on existing service.
- Which nodes/agents are in the first acceptance pilot.
- Whether public read-only publication is in scope for this feature or a separately approved feature.
- If/when P1 operator-triggered recovery can be enabled.

## Approval condition
This Markdown spec is a planning proposal. Before a behavior-changing Task starts, create/review an approved machine-readable specs/features/agent-observability-recovery.yaml with unique requirement and acceptance IDs, then link tasks through SpecGate. Do NOT mark a draft spec approved by default.
