# Agent Harness MVP: one task, no false RUNNING

This is an isolated **state-machine proof of concept**, not a production executor.
It belongs in `app/services/agent_harness_mvp.py` and uses only Python stdlib.
The API records evidence supplied by trusted callers; it **does not** launch
or terminate actual Claude/Codex processes or delete worktrees. No network,
secrets, background scheduling, or privileged permissions are required.

## Guarantees implemented

- Immutable main and child goal; one output/DoD; one idempotency key.
- Creation deadline at most 24h and refusal to dispatch once expired.
- `DISPATCHED` receipt never counts as `RUNNING_VERIFIED`.
- `RUNNING_VERIFIED` requires backend RUNNING, process alive, a fresh (<60m)
  progress marker, and a nonempty evidence reference.
- A shell returning to its prompt or an IDLE agent is `NOT_RUNNING`.
- `DONE_VERIFIED` requires artifact, test, and runtime readback references.
- Cleanup is `CLEANED_VERIFIED` only with independent stop/deletion evidence,
  no active task refs, preserved evidence, and a clean worktree.
- SQLite event log records the transitions for reconciliation after restart.

## Run tests

```bash
python -m pytest -q tests/test_agent_harness_mvp.py
```

## Not yet implemented / block before production

1. Terminal MCP adapter that supplies authenticated task receipts, process
   status and evidence. No attempt to fake this integration.
2. Background executor that invokes deadline reconciliation on schedule and
   a verified cancel/stop against actual process groups. Expiry state is not
   proof of termination.
3. Independent evidence verifier (not user-supplied arbitrary strings) and
   automated cleanup of approved temporary resources.

**Next acceptance test:** on a disposable repository, launch a harmless
Codex task via the existing Terminal MCP durable `start` path, record the
receipt, check real execution state in a later independent check, validate the
artifact/tests, then verify safe session cleanup. Production/approval gates
remain unchanged.
