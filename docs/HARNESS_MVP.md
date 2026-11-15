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
- The local unit suite passed **24/24 tests** on Dell after adding parent WIP
  limits and the expiry reconciliation CLI, and the branch checkout was clean.

## NOT YET a production unattended harness

The one-task execution smoke remains **operator-invoked**. A narrowly scoped
**staging systemd timer** on Dell now runs the SQLite deadline reconciler every
five minutes. It records `EXPIRED` and `CLEANUP_PENDING` durably; it does NOT
kill a task/process, create child monitors, run agent loops, integrate with
production ProjectFlow task queues, or prove long-running code/PR/deploy
acceptance. Real process group cancellation and authorized stop confirmation
remain separate future gates. Never call a
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

## Parent checkpoint and staging deadline timer (15 November 2026)

- Parent goal is immutable; the SQLite store enforces **at most 2 outstanding
  children** per main goal transactionally. A terminal child with
  `CLEANUP_PENDING` still occupies its slot; cloning/restarting it does not
  silently free capacity.
- `scripts/harness_reconcile.py` reads child task states and can mark all due
  jobs `EXPIRED` with cleanup still pending. Its default is read-only; write
  requires the explicit `--apply` flag. Repeated runs are idempotent.
- `scripts/harness_staging_timer.sh install|status|remove` manages ONLY the
  opt-in `projectflow-harness-reconcile-staging.timer` in the user's systemd
  instance on Dell; it requires a pre-existing staging SQLite DB and does not
  touch any other timers or production services. The current Dell staging timer
  is **enabled/active** with five-minute calendar cadence; the service's first
  actual invocation succeeded with exit code 0.
- `tests/test_agent_harness_reconcile_cli.py` exercises the real CLI against
  a disposable database, checking dry-run, expiry and no duplicate events.

Check/read a parent without changing state:

```bash
python scripts/harness_reconcile.py --main-goal 'MVP Harness: one task executes, verifies and cleans safely'
bash scripts/harness_staging_timer.sh status
```

For staging removal (retains SQLite events):

```bash
bash scripts/harness_staging_timer.sh remove
```

**Limit:** The 24h deadline is currently bounded by the next scheduler tick;
it does not prove that an already running CLI process was forcibly terminated
at exactly 24h. Never call `EXPIRED` a successful process cancellation.
