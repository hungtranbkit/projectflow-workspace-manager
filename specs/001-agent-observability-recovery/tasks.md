# Tasks: Agent Observability & Recovery

**Status:** Planned only; no code tasks dispatched by creating this document.
**Feature scope:** specs/001-agent-observability-recovery/spec.md
**Rules:** each atomic child task ≤1 day, each parent slice ≤2 days; one agent/branch/worktree; test and evidence required before progress advances. Do not use GitHub Actions paid minutes.

## Phase 0 — Spec Kit review and safe adoption (P0)
- [ ] T001 [P0] Audit current session/Terminal MCP/Harness read models; document which event and provenance fields already exist. DoD: source map with exact code references and supported/unsupported evidence levels.
- [ ] T002 [P0] Inspect CLI hooks for installed Claude/Codex versions in a disposable copy of settings; do not change owner settings. DoD: compatibility matrix and confirmed backup/restore procedure.
- [ ] T003 [P0] Review this spec for conflicts with AGENTS.md, Spec Layer, TaskDecisionService and known Harness contracts. DoD: resolved decisions or explicit blockers; no second scheduler.
- [ ] T004 [P0] Create the machine-readable draft FeatureSpec in specs/features only after confirming unique IDs and baseline impact; request approval before changing status to approved. DoD: SpecRegistry validation, tested SpecGate failure on draft, reviewed approval process.

## Phase 1 — Evidence collection (P0)
- [ ] T005 [P0] Add normalized event schema and provenance classification. DoD: schema tests for valid/missing/spoofed identities and secret redaction.
- [ ] T006 [P0] Add replay-safe ingest/readback adapter for existing session events. DoD: two replays/out-of-order fixture produce exactly one logical event per ID, no session relabeling.
- [ ] T007 [P0] Implement bounded event store/read model using the established persistence boundary. DoD: load/restart retention and bounded growth tests; no duplicate task scheduler.
- [ ] T008 [P0] Derive explicit dispatch/execution/acceptance dimensions with freshness. DoD: tests for disconnected node, queued receipt, live process, stale output, failed CLI, and independent acceptance.
- [ ] T009 [P0] Reuse Harness gate completion and immutable TTL views. DoD: synthetic tool-call bursts do not change the completion percent; expired/cleanup pending remains blocked.

## Phase 2 — Operator dashboard (P0/P1)
- [ ] T010 [P0] Implement bounded read-only observability endpoint(s) with cursor pagination and safe projection. DoD: API contract tests, no mutation from GET, latency measured.
- [ ] T011 [P0] Implement fleet/task/agent mobile cards and evidence timeline. DoD: operator can see verified/unverified/stale status and cause; 375/428 px smoke.
- [ ] T012 [P1] Add worktree/branch/file-touch and parent-child subagent maps through existing Git data. DoD: linked identities, no unsafe Git writes or public paths.
- [ ] T013 [P1] Add configurable STALLED/WAITING/BLOCKED diagnostics and next-safe-action hints. DoD: heartbeat-only process is not falsely called productive; no cleanup or restart.
- [ ] T014 [P1] Benchmark event-to-view delay and backpressure under 50 simultaneous event producers/reconnect bursts. DoD: recorded p95, bounded queue, test artifacts.

## Phase 3 — Guarded recovery (requires separate approval)
- [ ] T015 [P1] Design operator-confirmed idempotent retry/resume using existing ownership and execution guards. DoD: duplicate submit returns same tracked work, unsafe/unknown ownership refused, no auto merge/deploy.
- [ ] T016 [P1] Run a disposable recovery pilot with dead process, waiting input, stale receipt and node disconnect. DoD: independent acceptance/cleanup receipts with zero double starts.

## Phase 4 — Release gate, not auto-release
- [ ] T017 [P0] Run focused tests, local preflight/test, API/UX negative fixtures, and a second-agent audit. DoD: evidence-backed test report and review status, not a self-declared PASS.
- [ ] T018 [P1] Prepare a rollback/field allowlist and decide whether a sanitized public read-only view is separately approvable. DoD: privacy checklist, rollback drill, no unreviewed public deployment.

## Dependencies / execution order
T001–T003 → T004 approval → T005–T009 → T010–T011 → T012–T014 → (explicit approval) T015–T016 → T017–T018. T002 and T003 may be investigated in parallel; T005 schema must precede all adapters. Recovery and public serving are never implicit consequences of this spec.

## Status reporting
Report only: đã làm / đang làm / sẽ làm, verified gate %, child TTL % (distinct), blocker, evidence reference, and top three next evidence-producing priorities. QUEUED and DISPATCHED_UNVERIFIED never equal active coding. Unknown source is UNKNOWN rather than an optimistic percentage.
