# PROJECT_CONTEXT — projectflow-workspace-manager

## 2026-11-18 — Execution-first monitor dispatch (branch `agent/claude/monitor-durable-dispatch-118`)

- **Problem.** Monitors detected blockers and "decided" fixes, but no durable task was dispatched, so agents stayed IDLE. Terminal MCP 56ccb14 answered `start` with `task_id=null`. The bridge had no dispatch step: it could only `register` a task id obtained elsewhere. Its readback also rejected the Terminal MCP supervisor states.
- **Change.** In `scripts/harness_monitor_bridge.py`:
  - `dispatch_child` and the CLI action `dispatch`;
  - `local_mcp_turn`, a single local-only MCP caller;
  - `BACKEND_STATES`, which now includes the supervisor states;
  - `reconcile` maps `STUCK_COMPOSER`/`DISPATCH_UNCERTAIN` to BLOCKED;
  - new table `harness_dispatch_attempts` (outcomes DISPATCHED / DISPATCH_FAILED / ESCALATED, plus a deterministic escalation per error code).

  The contract is in `docs/HARNESS_MONITOR_INTEGRATION.md` ("Execution-first dispatch").
- **Invariants to preserve.**
  - A failed or untracked dispatch never creates a Harness task and never counts as work.
  - There is one deterministic request key per decision, and retries are bounded (3).
  - Refusals (paused, WIP, duplicate) happen before any send.
  - A finished remote task is never DONE_VERIFIED without independent DoD.
- **Dependency.** terminal-mcp PR #120: `start` must return a `dt_` id, and `task_status` must answer `dt_` ids. Without it, dispatch correctly reports `DISPATCH_FAILED/NULL_TASK_ID` → `UPGRADE_CONTROLLER_DURABLE_START`.
- **Tests.**
  - `tests/test_harness_monitor_dispatch.py` has 14 tests: success, null or untracked task_id ×4, busy with a bounded retry then escalation, retry with the same key, no send on restart replay, transport failure, readback mismatch, paused and WIP refusing before send, and supervisor-state reconcile. All fail on `origin/main` (RED) and pass with the change. Mutation: 5/5 guard mutants killed (accept untracked id, unbounded retry, resend on restart, skip readback, send before WIP check).
  - Added to the local-ci `remaining` tier. Results: `scripts/ci-test.sh remaining` 91 passed (rc=0), `fast` 33 passed (rc=0).
- **Not done.**
  - Not merged, not deployed, no live dispatch.
  - The bridge reaches only `http://127.0.0.1:8766/mcp` on the controller host.
  - Live use needs PR #120 deployed and an owner-approved run on HP.
