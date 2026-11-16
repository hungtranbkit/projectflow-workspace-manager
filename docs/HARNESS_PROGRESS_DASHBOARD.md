# Harness Progress Dashboard — 16/11/2026

## Two different progress meters

**Green / project verified completion:** percentage of independently evidenced
acceptance gates **PASSED** in the reviewed
`config/harness_progress_gates.json`. These are conservative, explicit weights
that total 100% for each project. Draft PRs, code lines, agent running, a passing
technical canary and build-only tests **never add to this score**. A change to a
gate from IN_PROGRESS/BLOCKED to PASSED requires a verifiable proof reference,
acceptance timestamp, and review/commit. Until then the same fixed percentage
is shown, regardless of how often the monitor ticks.

**Orange / each child task time:** `(observation - created_at) /
(expires_at - created_at)` capped at 100%. Shows **time consumed out of the
immutable hard TTL**, elapsed `h:mm`, remaining `h:mm`, deadline in ICT,
and overdue flag. A completed task stops consuming time at its recorded
verified timestamp; an expired task shows 100%. This meter is **not** % of code
completed. The 24h deadline is a maximum for a child, never a deadline for the
entire project.

The registered maintenance monitor has no completion percentage: its job is
ongoing. A project without a reviewed gate plan must show UNKNOWN rather than
a fabricated percentage. Paused automations remain paused.

## Current conservative baseline

- **Zalo Hub V2/V229: 10% gate-verified**: user-selected V229 architecture
  only. A1a/A1b journal, missed-text R1/R2, unified UI lease and S7 independent
  receiver proof are NOT accepted. A passing Node/JDK21 Harness QA does not
  prove forward parity on the physical S7.
- **CDTM: 0% gate-verified**: missing licensed/public merchandise feed.
- **Screen Remote: 0% gate-verified**: native iPhone/NEC audio/video acceptance
  not yet independently confirmed.
- **Terminal MCP session cleanup:** continuous maintenance; N/A.

These weights are **a reviewed planning estimate**, not an objective measure
of engineering hours, success probability, or percentage of source code written.
Review the gate checklist when new independent evidence arrives.

## Dell readback (no agent dispatch or network)

~~~bash
cd /home/dell/workspace/projectflow-harness-main-20261115
PF=/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python
$PF scripts/harness_progress.py --format markdown
$PF scripts/harness_progress.py --format json
$PF scripts/harness_progress.py --write --format markdown
~~~

The `--write` option creates owner-only private files in the existing SQLite
state directory:

- `~/.local/state/projectflow-harness/monitor-progress.json` (machine-readable)
- `~/.local/state/projectflow-harness/monitor-progress.html` (mobile-responsive
  single page with green gate and amber task deadline bars)

Both are **private artifacts**, not a public unauthenticated dashboard. Serving
them externally requires a separately reviewed authenticated read-only route.
The existing Harness timer will refresh both every five minutes with the
`--emit-progress` flag after core and bridge reconciliation. The combined
service remains fail closed if the progress snapshot cannot be written.

## How scheduled monitor reporting should work

Each active monitor reads its exact `automation_id` from the shared JSON and
reports three values: `completion_percent`, `wip/max_wip`, and for each
active child `time_used_percent`, `time_used_label`,
`remaining_label`, `deadline_local`. Include one current blocker and the
next evidence-producing action. **Do not calculate a new percentage by opinion**
during an automation tick. When the evidence changes, update the reviewed gate
policy before treating the new percentage as authoritative.

Monitor reporters may use an inline textual bar such as
`█░░░░░░░░░░░ 10%` plus a separate clock bar; label them clearly.
If a task has already finished, show `đã kết thúc` rather than a negative
remaining clock. For backend errors or an outdated health receipt, mark health
UNKNOWN/FAILED_OR_STALE; never repeat the last green result as current.
