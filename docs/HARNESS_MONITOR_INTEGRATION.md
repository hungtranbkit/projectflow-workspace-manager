# Harness Monitor Bridge — 16/11/2026

The Harness release on Dell tracks the user's existing ChatGPT automations without
creating duplicate schedules or changing a paused task's enabled state.

## Inventory and current activation

The versioned file `config/harness_monitor_registry.json` inventories **25 existing
automations**. Three were enabled when imported:
- CDTM — goal: actual legal sellable product sources and evidence-based Facebook eligibility
- Screen Remote — goal: real iPhone/NEC/Brave YouTube native runtime acceptance
- Terminal MCP cleanup — goal: safely remove truly finished sessions without losing work

All other 22 are **PAUSED**. BrandAgent, Zalo Hub and Offline POS are among
those paused. Registration **never** resumes them. Reactivation is a separate
user action and requires refreshing the immutable goal binding.

## Dell operations

The bridge uses the same production Harness SQLite DB, not a separate task DB.
Use a clean checkout of the deployed ProjectFlow main branch.

~~~bash
cd /home/dell/workspace/projectflow-harness-main-20261115
PF=/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python
TMCP=/home/dell/workspace/terminal-mcp/.venv/bin/python

# Idempotent first registration: 25 bindings, only 3 active
$PF scripts/harness_monitor_bridge.py bootstrap
$PF scripts/harness_monitor_bridge.py status
$PF scripts/harness_monitor_bridge.py status --automation-id 6af698c9a5308191899c6a7e013cee5f

# Read-only reconciliation of linked Terminal MCP task states.
$TMCP scripts/harness_monitor_bridge.py reconcile
~~~

The currently deployed `projectflow-harness-reconcile-staging.timer` runs every
five minutes. Integrate the additional bridge `reconcile` invocation into the
same service, not a second unlimited scheduled worker.

## Register a real child receipt

A monitor must inspect existing agent sessions/worktrees **first** and avoid
duplicate agent dispatch. Use the authoritative connected Terminal MCP
`terminal_turn(action=start)`, keeping its durable `task_id`. No task
receipt equals a running or finished worker.

Terminal MCP's *local Dell endpoint* cannot read all durable task IDs from the
ChatGPT-level central controller (observed `TASK_NOT_FOUND` for a task that
ChatGPT's Terminal MCP could read). There are two explicit modes:

- `register`: verified against the local MCP server. Rejects missing tasks.
- `register-attested`: caller supplies a **sanitized readback** produced by
  the central Terminal MCP tool, validated for matching task ID, target/session
  and agent. The provenance is `CENTRAL_ATTESTED`, **not** cryptographic
  verification. This records DISPATCHED only, never RUNNING_VERIFIED or DONE.

After a subsequent central tool check, `observe-attested` may record
NOT_RUNNING/BLOCKED/UNKNOWN/FAILED, but cannot certify RUNNING or business DONE
from forwarded JSON. Every failed task keeps `CLEANUP_PENDING` and occupies
the WIP slot. An actual project-specific independent acceptance verifier is
required before `DONE_VERIFIED`.

Example when a monitor already has a trusted Terminal MCP tool readback:

~~~bash
$TMCP scripts/harness_monitor_bridge.py register-attested \
  --automation-id '<exact id>' --task-id 'dt_...' \
  --session '<exact terminal session>' --agent codex --node dell-linux \
  --child-goal 'One atomic result' --dod 'One independent pass condition' \
  --ttl-seconds 86400 \
  --attestation-json '{"status":"OK","result":{"task":{"task_id":"dt_...","target":"<exact terminal session>","agent":"codex","mode":"agent","state":"RUNNING"}}}'
~~~

Never fabricate this JSON, and never copy secrets, account data or agent pane
content into it. If a valid central readback cannot be obtained, leave the
task UNREGISTERED/UNKNOWN; the original monitor can continue read-only
investigation, but must not report a Harness-enforced child as running.

## Guards

- Each enabled parent's main goal is pinned immutably with SHA-256 in SQLite.
  A changed goal/ref without an explicit migration fails closed.
- At most **two outstanding child tasks per main goal**; registered receipts
  and pending cleanup consume capacity. Task TTL must be 1–86400 seconds and
  cannot be reset by another check.
- The Terminal MCP cleanup automation has `observe_only` mode, so it cannot
  spawn new Harness child tasks.
- The bridge does not send messages, manipulate Brave/NEC, spend money, grant
  Zalo/Meta permissions, merge/deploy projects or destroy sessions.
- Paused automations are an **inventory snapshot**, not executable schedules.
  The actual ChatGPT automation settings remain authoritative.
- There is no unattended project-specific runtime acceptance for all 25
  automations. Only the Harness-owned coding pilot has E2E acceptance; wider
  multi-project acceptance requires each monitor's own verified DoD.

Tracking branch: `feat/harness-monitor-integration-20261116` in
`hungtranbkit/projectflow-workspace-manager`.


## Independent technical monitor-to-Harness acceptance (one-shot)

Run this only on trusted Dell, from a CLEAN deployed main checkout with the
existing Harness SQLite DB and user-systemd timer:

~~~bash
PF=/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python
MAIN=/home/dell/workspace/projectflow-harness-main-20261115
cd "$MAIN"
$PF scripts/harness_monitor_canary.py --monitor-id 6af65f5c6cdc8191a2ecc2f47085bea6 --launch
$PF scripts/harness_monitor_canary.py --monitor-id 6af698c9a5308191899c6a7e013cee5f --launch
systemctl --user start projectflow-harness-reconcile-staging.service
$PF scripts/harness_monitor_bridge.py status
~~~

Each task writes a unique marker using the existing systemd-owned echo-probe
executor, testing only the monitor-to-immutable-goal-to-checkpoint and cleanup.
Child goal includes CANARY ONLY and project_DoD is NOT_TESTED_BY_CANARY.
It is NOT a CDTM source-license approval or a Screen Remote native hardware
acceptance, and it cannot authorize Facebook posts, browser control, spending,
permissions or a downstream merge/deploy.

Repeated launches never duplicate the same canary. Paused monitors and the
maintenance cleanup monitor are forbidden to start a canary.

## Fail-closed WIP invariant

A task reaching DONE_VERIFIED or FAILED now becomes CLEANUP_PENDING immediately;
it still consumes one of two parent WIP slots until independent cleanup proof.
EXPIRED behaves likewise. Even after cleanup, repeating the same child goal
with a new key is refused; a genuinely new scoped goal is required.

The service does not infer business completion from these canaries. Terminal
MCP controller task IDs may be invisible to the Dell local MCP endpoint;
CENTRAL_ATTESTED receipts are never cryptographic RUNNING/DONE proof.

## Durable managed dispatch when Terminal MCP queue is disabled (16/11/2026)

The Terminal MCP implementation maps `start` → `send` when its durable
queue feature is disabled. A returned `SUBMIT_CONFIRMED` in that mode **does
not contain a durable task_id**. Do not register it as an external Harness
child or call it RUNNING. This is expected server behavior, not proof that the
agent finished. The monitored project goal stays unchanged.

A reviewed enabled parent can now dispatch directly to Harness's durable,
systemd-owned local executor with a short CLI invocation; Terminal MCP only
transports this invocation. The Harness SQLite task ID and systemd unit are the
source of truth. The command refuses paused/maintenance monitors, a project
origin that differs from the locked registry, paths outside Dell workspace,
and anything other than a clean isolated harness/* branch. WIP and TTL guards
are provided by the same HarnessStore as other monitor children.

Safe technical read-only Codex E2E, with NO project business acceptance:

~~~bash
cd /home/dell/workspace/projectflow-harness-main-20261115
PF=/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python
$PF scripts/harness_monitor_bridge.py launch-managed \
  --automation-id 6af698c9a5308191899c6a7e013cee5f \
  --child-goal 'CANARY ONLY: verify Screen Remote read-only Codex handoff' \
  --repo-dir /home/dell/workspace/harness-screen-agentpilot-20261116 \
  --executor codex \
  --proof-token HARNESS_SCREEN_REMOTE_CODEX_BRIDGE_20261116 \
  --ttl-seconds 180
systemctl --user start projectflow-harness-reconcile-staging.service
$PF scripts/harness_monitor_bridge.py status \
  --automation-id 6af698c9a5308191899c6a7e013cee5f
~~~

Only report DONE_VERIFIED + CLEANED_VERIFIED if the service has verified the
actual Codex output, unit completion, artifact hashes and cleanup. Even then
project_DoD remains NOT_TESTED_BY_CANARY: physical iPhone/NEC/Brave video and
audio acceptance is separate.

For a genuine atomic implementation task on an already-existing pytest
acceptance, use `--executor codex-task --test-path tests/test_*.py
--allowed-file existing_implementation.py`; this is restricted to a clean
harness/* branch of the reviewed monitor's exact GitHub repo and **does not
push/merge/deploy**. Only use it when a monitor has an explicit specific bug
and independent DoD. CDTM is primarily TypeScript and cannot use this pytest
executor directly; do not invent a Python acceptance test to bypass the gate.

At each monitor turn, check existing Harness status and existing agent
sessions before launching. Do not spawn canaries repeatedly or use a completed
technical probe as a business milestone.
