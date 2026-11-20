# Implementation Plan: Agent Observability & Recovery

**Status:** Proposed; no implementation or deployment authorized by this plan.
**Feature:** specs/001-agent-observability-recovery/spec.md

## Existing architecture constraints
- Keep Task, Workspace, AgentSession and TaskDecisionService as authorities for identity and execution/acceptance status. Existing behavior: specs/features/agent-execution.yaml.
- Reuse Harness monitor bridge, status/health snapshots, gate-weighted progress and child TTL. References: docs/HARNESS_MONITOR_INTEGRATION.md and docs/HARNESS_PROGRESS_DASHBOARD.md.
- Existing worktree policy: ONE AGENT → ONE BRANCH → ONE WORKTREE; integration PR/CI and exact tested HEAD remain independent.
- Production deployment is disabled in PROJECT.yaml for this repository. No auto-push, main merge or deploy.
- SpecRegistry baseline digest covers YAML under specs/, so do not introduce a new approved FeatureSpec until review and traceability are ready.

## Proposed design (bounded extension, not a replacement)

### A. Collect
Start with read-only adapters for the actual available Terminal MCP session/task readbacks and existing AgentSession lifecycle. Add opt-in CLI hooks only after testing against installed CLI versions and backing up settings. No assumption that an attested central receipt is cryptographically verified.

### B. Normalize
Normalize each activity into an envelope with version, immutable identity, timestamps, tool kind, parent-child agent link, source/provenance and redacted evidence pointer. Treat missing identity as quarantined. Validate schema, deduplicate event_id/dedupe_key, record late arrivals and preserve monotonic observed_at; occurred_at is display time, not an excuse to reorder acceptance states.

### C. Derive read models
Compute independently:
- **Liveness** from owned process/session proof and freshness (stale → UNKNOWN).
- **Activity** from tool calls/output growth. A heartbeat alone is not progress.
- **Business completion** exclusively from reviewed acceptance gate evidence.
- **Time budget** from immutable child TTL.
- **Blockers** from waiting for input, stalled output, failed tests, expired/cleanup pending, lost node.

Read existing Harness status data rather than duplicating its scheduler or progress DB; decide whether to add a small namespaced event table to the existing SQLite store only after inspecting persistence boundaries.

### D. Deliver
Create a bounded read-only API/read-model on the existing operator service. Consider SSE only after server/consumer lifetime and backpressure are tested; polling with ETag/cursor is an acceptable initial fallback. Mobile-first dashboard: fleet summary → project → task → agent and evidence timeline. Distinguish verified and unverified badges using text, not color alone.

### E. Recovery — separate P1 gate
Only after A–D acceptance, expose operator-approved retry/resume for known owned resources through existing idempotent task APIs. No arbitrary SSH/script submission from the browser, no unknown session termination, no new scheduler, no cross-project duplicate dispatch.

## Event/API contract draft
Private normalized event:
- required: event_id, schema_version, source, occurred_at, observed_at, node_id, project_id, task_id, session_id, agent_id, kind, provenance_level, dedupe_key
- optional: parent_agent_id, worktree_ref, branch_ref, sanitized_evidence_ref, result, redaction_flags
- no raw prompt, provider credential, full terminal content, internal host address, or arbitrary shell command in the public model

Read endpoints are proposals, not existing routes:
- GET /api/observability/projects — bounded summary by allowed project
- GET /api/observability/tasks/{task_id} — freshness, independent state dimensions and verified gates
- GET /api/observability/tasks/{task_id}/events?cursor=...&limit=... — bounded, paged, sanitized timeline
- GET /api/observability/health — source freshness and adapter health, not business-success claims
Avoid adding any write route in P0.

## Test strategy / quality gates
1. Fixtures for CLI event kinds, parent/child linkage, missing ID, replay, out-of-order arrival, duplicate message and malicious/sensitive payloads.
2. Read-only tests proving no process gets started by dashboard GET requests.
3. Integration tests for STARTING/RUNNING/WAITING_FOR_INPUT/EXITED transitions with same workspace identity and fail-closed source loss.
4. Property tests ensuring activity volume never changes gate completion.
5. Simulated disconnect/reconnect and bounded backlog catch-up; stale state cannot stay green.
6. Concurrency tests for duplicated dispatch/resume; existing guard preserved.
7. Mobile screenshot/playwright smoke at 375 and 428 px and operator evidence drill-down.
8. Regression through ./scripts/preflight.sh and ./scripts/test.sh locally. GitHub Actions minutes are not a release dependency here.
9. Deployment only after explicit separate authorization; privacy review for any public view.

## Rollout and rollback
Pilot 1: existing locally available Codex/Claude session readbacks only, read-only, no settings changes.
Pilot 2: opt-in hooks on one verified node with settings backup, bounded event cache, dual readback comparison.
Pilot 3: multi-node offline/reconnect acceptance, operator-only dashboard.
Pilot 4: only if separately approved, sanitized public read-only projection.
Rollback: disable adapter/hook, restore original backed-up CLI settings, stop collector without impacting active CLI sessions, retain audit evidence and old Harness snapshots.

## Risks and mitigations
- **False running:** require actual process/tool proof, not merely successful prompt send.
- **False progress:** use approved gate weights and independent evidence.
- **Attested vs verified receipts:** store provenance; central-attested does not imply DONE.
- **Hook incompatibility:** version matrix, opt-in, backup/restore, fallback to read-only.
- **Duplicate dispatch:** immutable task/session keys, existing single-execution guard.
- **Disk growth:** bounded retention, aggregation and audited limits.
- **Private data leak:** schema allowlist, split public projection, negative-field tests.
- **UI-only success theater:** count accepted gates and real blocked work, not animations.

## Completion definition
Feature implementation is accepted only when all linked machine-readable FeatureSpec AC IDs pass independently, test suites pass, project review/integration gates pass, and deployment is separately accepted if requested. A draft plan or PR is never DONE.
