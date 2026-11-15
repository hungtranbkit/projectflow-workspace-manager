# Agent Harness MVP — one task, real evidence

**Scope:** One atomic child task with immutable main goal, SQLite checkpoint,
24-hour maximum declared deadline, safe dispatch classification, independently
verified acceptance, and verified cleanup. No production changes or automatic
permissions. See PR #40 and Issue #39.

## What is implemented

- `app/services/agent_harness_mvp.py`: persistent state and event log,
  reject duplicate live goals, refuse late dispatches, distinguish
  `DISPATCHED` / `RUNNING_VERIFIED` / `NOT_RUNNING` / `BLOCKED` /
  `DONE_VERIFIED`. A process that finished quickly may be
  `NOT_RUNNING` at inspection and still be accepted **only** with
  independently validated artifact, test proof, and runtime readback.
- `app/services/agent_harness_terminal.py`: reusable authorized MCP adapter
  for session creation, durable dispatch, bounded status readback and
  confirmed cleanup. It refuses to delete sessions it did not create and
  refuses deletion unless they are safely IDLE. The backend verifies task
  references and tmux tombstones; the adapter checks session absence.
- `scripts/harness_live_smoke.py`: local E2E smoke runner. Creates **its own**
  disposable shell session, invokes either a harmless shell command or a
  real **Codex CLI** command with read-only sandbox, verifies output and
  artifact, accepts the child task, deletes the owned session via the
  officially supported `terminal_turn(args={"confirm":true})` and
  removes the temporary output. The SQLite evidence remains.
- `tests/test_agent_harness_mvp.py` and
  `tests/test_agent_harness_terminal.py`: fail-closed unit tests.

## Dell usage (operator-controlled)

Run on the Dell with the already installed ProjectFlow and Terminal MCP venvs:

```bash
cd /home/dell/workspace/harness-mvp-impl-20261115
PYTHONPATH=. /home/dell/workspace/projectflow-workspace-manager/.venv/bin/python \
    -m pytest -q tests/test_agent_harness_mvp.py tests/test_agent_harness_terminal.py

# Check local MCP availability, read-only:
/home/dell/workspace/terminal-mcp/.venv/bin/python scripts/harness_mcp_probe.py

# Real Codex agent smoke, read-only and no login/trust bypass:
HARNESS_EXECUTOR=codex \
  /home/dell/workspace/terminal-mcp/.venv/bin/python \
  scripts/harness_live_smoke.py

# Shell-only smoke:
HARNESS_EXECUTOR=shell \
  /home/dell/workspace/terminal-mcp/.venv/bin/python \
  scripts/harness_live_smoke.py
```

The Codex smoke needs an isolated clone at
`/home/dell/workspace/harness-mvp-impl-20261115` (override with
`HARNESS_CODEX_WORKDIR`). The local MCP URL defaults to
`http://127.0.0.1:8766/mcp` (override with `HARNESS_MCP_URL`);
**do not expose this endpoint**.

## Live evidence — 15 November 2026

- Official Terminal MCP client established connection; 340 tools returned.
- Dell shell executor: `DONE_VERIFIED`, `CLEANED_VERIFIED`, and
  `artifact_removed=true` in the updated runner.
- Real Codex CLI v0.166.0 (`--sandbox read-only`) answered the unique
  challenge. In the **same task lifecycle**, Harness independently read the
  Codex `--output-last-message` file and session output, verified its SHA,
  accepted the task and confirmed session deletion:
  `executor=codex`, `DONE_VERIFIED`, `CLEANED_VERIFIED`,
  `artifact_removed=true`; `harness_task_id=2`.
- The local unit suite passed **17/17 tests** on Dell again after the
  Codex-mode smoke patch, and the branch checkout was clean.

## NOT YET a production unattended harness

This MVP is **operator-invoked**, not a continuously running executor or a
replacement for ChatGPT Automations. It does **not** yet provision child
monitors, enforce real process termination exactly at 24 hours, integrate
ProjectFlow task queues, or prove a long-running coding/PR/deploy acceptance.
The task TTL stops a late dispatch when reconciled; an actual watchdog and
authorized cancel confirmation are separate future gates. Never call a
submitted receipt `RUNNING` without observed live activity.

Do not merge/deploy to production, grant broad rights, modify Codex trust,
delete unrelated sessions, or claim full-monitoring integration based only
on this smoke result.

### Cleanup limitations

The smoke creates and deletes only its own `harness-smoke-*` shell session.
A separate manually created Codex session `harness-codex-live-20261115`
remains blocked on the workspace-trust prompt and is **not** part of the
successful smoke. The official MCP tool classifies it as WAITING_INPUT/UNKNOWN
according to observation depth; the safe cleanup helper refuses uncertain
states. Do not report it deleted or auto-approve its trust prompt.
Direct ChatGPT `terminal_turn` still lacks an exposed confirmation field;
tracked in [Terminal MCP #119](https://github.com/hungtranbkit/terminal-mcp/issues/119).
The local SDK exposes `args={"confirm":true}` and the smoke has verified it.
