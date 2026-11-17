# Harness Meta-Monitor P0 — L1/L2/L3 evidence ledger (issue #52)

**Status: CODE DONE, runtime acceptance NOT PROVEN.** The QUALITY CONTROL +
CONVERGENCE V2 automation prompt (17/11/2026) is configuration, not backend
enforcement. This module only makes the loop auditable. It can be called
"proven" only after real Meta-Monitor cycles have been recorded here and show
measured improvement.

Implementation: `scripts/harness_meta_monitor.py`; tests:
`tests/test_harness_meta_monitor.py` (part of `scripts/ci-test.sh fast`).

## Boundaries (do not relax)
- Separate owner-only ledger `~/.local/state/projectflow-harness/meta-monitor.db`
  (file 0600, directory 0700; a group/other-readable ledger is refused). It is
  **append-only**: SQLite triggers abort every UPDATE/DELETE. It never writes
  the production Harness DB.
- It changes nothing: no automation prompt, schedule, enabled state, goal,
  gate policy, branch protection, production runtime or monitor children.
  A rollback is only a *proposal*.
- Free text is limited to short references and rejected when it looks like a
  secret (`secret_redaction.redact`). Do not record customer data or pane dumps.

## Three layers
| Layer | Source | Missing/stale evidence |
|---|---|---|
| L1 business outcome | fresh gate checkpoint via `harness_monitor_report.report` (percent, gates, 7d gate delta) | `UNKNOWN` |
| L2 monitor effectiveness | recorded cycle observations: `useful_action_rate`, `duplicate_dispatch_count`, `no_delta_cycles`, blocker code/class/age, `owner_interventions`, `stale_readback_rate`, `cleanup_pending_age_seconds` | `null`, never 0 |
| L3 rule quality | rule-experiment ledger: KEEP/REVERT/INCONCLUSIVE counts, revert rate, overdue checkpoints | `null` |

Useful work = a **fresh** readback (≤900 s, `VERIFIED_READBACK`) with an
execution or gate delta from `GATE_ACCEPTANCE`, `VERIFIED_TASK_OUTPUT` or
`INDEPENDENT_TEST` plus an evidence reference, and not a repeated dispatch key.
`CRON_COMPLETED`, `COMMIT`, `PR_OPENED`, `CANARY`, `HARNESS_WIP` and
`SELF_REPORT` claims are stored but counted as `non_evidence_delta_claims`,
never as useful work.

## Blockers: external vs orchestration
Blockers use structured codes (`BLOCKER_CLASSES`): EXTERNAL_OWNER (consent,
rights, device, platform permission, billing, credential, licence),
PRODUCT_DEFECT and ORCHESTRATION. An unlisted code is `UNKNOWN`; free text is
never regex-classified. `next_step()` returns `HOLD_OWNER_ACTION` under an
external blocker: a flat gate % is not a reason to pivot. Opening a rule
experiment on such a monitor is refused (`EXTERNAL_BLOCKER_NO_RULE_CHURN`).
PRODUCT_DEFECT → `FIX_PRODUCT`. ORCHESTRATION with 2 or 3 or more fresh
no-delta cycles → `DIAGNOSE` / `PIVOT`.

## Rule experiments
- Exactly one variable per experiment (`diff` has one key with before/after),
  distinct prompt sha256 before/after, and **one open experiment per monitor**.
  `rule_id@version` is immutable; a change needs a new version.
- The before-metrics are computed from cycles recorded before `applied_at`.
  If there are none, the before rate is `UNKNOWN` and the verdict can only be
  INCONCLUSIVE.
- `experiment-observe` snapshots metrics only from **new** cycles after the
  change. It refuses when there is no new cycle since the previous
  observation. The 24h/72h/7d checkpoints refuse before they are due, and a
  missed checkpoint shows as `OVERDUE_NO_SAMPLE` (never fabricated).
- KEEP/REVERT need at least 2 new observations, and both must agree with the
  evidence (±0.10 useful-action-rate margin). An operator verdict that
  contradicts the evidence is refused. INCONCLUSIVE is always allowed.
- `rollback-check` proposes a rollback only when the live prompt digest still
  equals the experiment's after-digest. Otherwise it returns
  `CONCURRENT_EDIT_DETECTED` (manual merge, never overwrite).

## Convergence
`STABLE_MAINTENANCE` requires all of the following:
- fresh measured cycles spanning 7 days up to the last 24 hours, with a
  measured action outcome;
- a guard run that passed within 24 hours (`guard-run` executes the local
  suite itself and records the commit and exit code);
- no orchestration defect or orchestration blocker in the last 7 days;
- no unresolved CRITICAL/SERIOUS defect.

An open **verified** regression → `REGRESSION_ACTIVE`, which leaves the stable
state. An unverified claim is surfaced but does not flip state. Missing
evidence → `UNKNOWN`.

## CLI (read-only report; append-only writes)
~~~bash
PF=/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python
$PF scripts/harness_meta_monitor.py report                       # one line META_MONITOR=...
$PF scripts/harness_meta_monitor.py cycle-record --json '{"monitor_id":"<id>","cycle_at":"<iso+tz>","readback_status":"VERIFIED_READBACK","readback_age_seconds":60,"execution_delta":false,"gate_delta":false,"blocker_code":"OWNER_CONSENT","blocker_since":"<iso+tz>"}'
$PF scripts/harness_meta_monitor.py experiment-open --json '<spec>'
$PF scripts/harness_meta_monitor.py experiment-observe --experiment-id rule@v2 --checkpoint 24h
$PF scripts/harness_meta_monitor.py experiment-verdict --experiment-id rule@v2 --verdict INCONCLUSIVE --rationale '...'
$PF scripts/harness_meta_monitor.py rollback-check --experiment-id rule@v2 --current-prompt-sha256 <sha>
$PF scripts/harness_meta_monitor.py guard-run
~~~
Exit 2 + `REFUSED` on invalid input. No HTTP endpoint and no public dashboard.

## Not done (next)
1. Wire the running Meta-Monitor prompt/cycle to call `cycle-record` once per
   cycle with its real readback. Until then the ledger is empty and every
   metric is `UNKNOWN`.
2. Decide who records `defect-record` events, and whether `guard-run` joins
   the existing reconcile service (a schedule change needs owner approval).
3. Runtime acceptance: at least 7 days of real cycles plus one judged
   experiment, before claiming improvement.
