# T001 — Source map: existing session / Harness / evidence read models

**Date:** 2026-11-20 · **Status:** DRAFT evidence for review (T001 DoD) · **Branch:** `feat/agent-observability-p0-nec-20261120`

Which observability fields already exist, where, and at what provenance
level the P0 adapters (`app/services/agent_observability.py`) may classify
them. Anything not listed here is **unsupported → UNKNOWN**.

## 1. ProjectFlow AgentSession lifecycle (app DB)

| Field / fact | Code reference | Provenance (ceiling) | Used by adapter as |
| --- | --- | --- | --- |
| `agent_sessions.id/task_id/workspace_id/agent` | `app/db.py` (CREATE TABLE agent_sessions) | LOCAL_OBSERVED | identity: `pf-session-{id}`, `pf-task-{task_id}`, `pf-workspace-{workspace_id}` (opaque, no path) |
| `status` STARTING→RUNNING→EXITED/FAILED | `app/services/agent_session_manager.py` `start()` / `on_exit` | LOCAL_OBSERVED | `session_started`, `process_exited`, `process_failed` |
| `last_activity_at` (PTY output) | `agent_session_manager.py` `on_activity()` | LOCAL_OBSERVED | `output_growth` (only while RUNNING/WAITING_FOR_INPUT) |
| `WAITING_FOR_INPUT` | reconciled in `reconcile_on_startup()` | LOCAL_OBSERVED | `waiting_input` |
| `exit_code`, `exited_at` | `on_exit` | LOCAL_OBSERVED | result PASS/FAIL on exit |
| `cwd`, `transcript_tail`, prompt columns | same table | **never read** | — (private: paths, terminal output, prompt) |
| process restart semantics | `reconcile_on_startup()` marks live rows FAILED | LOCAL_OBSERVED | → `process_failed` (not silently "running") |

## 2. ProjectFlow evidence tables (app DB)

| Table | Status values used | Provenance | Milestone |
| --- | --- | --- | --- |
| `test_runs` (`workspace_type='agent'`) | PASS / FAIL / ERROR | LOCAL_VERIFIED | `tests_passed` |
| `review_runs` | PASS / FIX_REQUIRED | LOCAL_VERIFIED | `review_passed` |
| `qa_runs` | PASS / FAIL | LOCAL_VERIFIED | `qa_passed` |
| `merge_records` | MERGED | LOCAL_VERIFIED | `merged` |
| `deployments` | VERIFIED / FAILED / ROLLED_BACK | LOCAL_VERIFIED | `deployed` |

`stdout_tail`, `stderr_tail`, `findings`, `notes`, `command`, `deployed_url`
are **never selected**. Only `table:id` is kept as `sanitized_evidence_ref`.
Task-level READY_FOR_MAIN / DONE remain computed by `TaskDecisionService`
(not duplicated). `accepted` can only come from `INDEPENDENT_ACCEPTANCE`
provenance; no existing ProjectFlow table is mapped to it in P0.

## 3. Harness SQLite ledger (`app/services/agent_harness_mvp.py`)

| Harness fact | Reference | Provenance | Mapped kind |
| --- | --- | --- | --- |
| `DISPATCHED` event | `HarnessStore.dispatch()` | CENTRAL_ATTESTED (receipt only) | `prompt_submitted` → DISPATCHED_UNVERIFIED |
| `OBSERVED` state=RUNNING_VERIFIED | `HarnessStore.observe()` requires live process **and** progress | LOCAL_OBSERVED | `output_growth` |
| `OBSERVED` state=STALLED | live process without recent progress | LOCAL_OBSERVED | `heartbeat` (liveness ≠ progress) |
| `OBSERVED` NOT_RUNNING / BLOCKED / UNKNOWN | `observe()` | LOCAL_OBSERVED | `process_exited` / `waiting_input` / `status_unknown` |
| `FAILED`, `EXPIRED` | `fail()`, `reconcile_due()` | LOCAL_OBSERVED | `process_failed` |
| `DONE_VERIFIED`, cleanup states | `accept()`, `cleanup()` | technical only | **not** mapped to acceptance; shown via TTL/cleanup |

## 4. Harness progress / TTL / health (reused, not recomputed)

| Fact | Reference | How P0 uses it |
| --- | --- | --- |
| Gate-weighted completion % | `scripts/harness_progress.py::read_gates`, `summarize()` + `config/harness_progress_gates.json` | `attach_harness()` copies `completion_percent` verbatim |
| Child TTL clock | `harness_progress.task_clock()` | copied into `time_budget`; EXPIRED/CLEANUP_PENDING → blocker |
| Service health | `monitor-health.json` via `summarize()["health"]` | not HEALTHY or snapshot > 900 s → completion UNKNOWN |
| Scheduler | `projectflow-harness-reconcile-staging.timer` → `scripts/harness_monitor_service.py` | **unchanged**; P0 adds no timer/worker |

## 5. Terminal MCP

Central controller readbacks exist only as caller-supplied
`register-attested` JSON (`scripts/harness_monitor_bridge.py`). P0 caps
source `terminal_mcp_central` at CENTRAL_ATTESTED: it may move a task to
DISPATCHED_UNVERIFIED or surface a negative reason, never RUNNING_VERIFIED
or any milestone. No live Terminal MCP adapter is implemented in P0.

## 6. CLI hooks (Claude/Codex)

Not inspected/changed (T002 not in this slice). Source `cli_hook` exists in
the schema with ceiling LOCAL_OBSERVED, but nothing emits it yet.
