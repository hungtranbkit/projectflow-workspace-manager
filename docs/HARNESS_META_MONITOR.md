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
$PF scripts/harness_meta_monitor.py ingest                       # real Harness record -> evidence cycles
$PF scripts/harness_meta_monitor.py report                       # one line META_MONITOR=...
$PF scripts/harness_meta_monitor.py cycle-record --json '{"monitor_id":"<id>","cycle_at":"<iso+tz>","readback_status":"VERIFIED_READBACK","readback_age_seconds":60,"execution_delta":false,"gate_delta":false,"blocker_code":"OWNER_CONSENT","blocker_since":"<iso+tz>"}'
$PF scripts/harness_meta_monitor.py experiment-open --json '<spec>'
$PF scripts/harness_meta_monitor.py experiment-observe --experiment-id rule@v2 --checkpoint 24h
$PF scripts/harness_meta_monitor.py experiment-verdict --experiment-id rule@v2 --verdict INCONCLUSIVE --rationale '...'
$PF scripts/harness_meta_monitor.py rollback-check --experiment-id rule@v2 --current-prompt-sha256 <sha>
$PF scripts/harness_meta_monitor.py guard-run
~~~
Exit 2 + `REFUSED` on invalid input. No HTTP endpoint and no public dashboard.

## Evidence ingest (GAP_OBSERVABILITY)
`ingest` turns the **real** Harness record into cycles. Pipeline:
`harness_monitor_service --emit-progress` → `monitor-progress.json` →
`harness_meta_monitor.py ingest` → ledger → `report`.
- **Which monitors:** one cycle per *enabled* automation in the versioned
  registry. Paused or unknown ids are skipped and not recorded.
- **Freshness:** checked through `harness_monitor_report.report` (≤900 s and
  health `HEALTHY`). A fresh cycle uses the snapshot's `observed_at` as its
  `cycle_at`. A stale or unhealthy readback is recorded as `HARNESS_UNKNOWN`
  in a 5-minute bucket, with every delta null.
- **Dedupe:** the cycle key is `(monitor, cycle_at)` plus a content sha256.
  An identical redelivery returns `ALREADY_RECORDED`. A different payload for
  the same key is refused (`CONFLICT`), and so is a snapshot older than the
  last ingested one (`OUT_OF_ORDER`).
- **Delta between two fresh cycles:**

  | Observation | Recorded as |
  |---|---|
  | New PASSED gate that has `proof` | `gate_delta`, source `GATE_ACCEPTANCE` |
  | New PASSED gate without proof | source `SELF_REPORT` (not useful) |
  | Task reached `DONE_VERIFIED` in the window | `execution_delta`, source `VERIFIED_TASK_OUTPUT` |
  | `CANARY ONLY` task finished | source `CANARY` (not useful) |
  | PASSED gate lost | `gate_regressed` flag only |

  The first cycle has no previous cycle to compare against, so both deltas
  are null (UNKNOWN).
- **Blockers:** not classified. The gate policy has only free-text `BLOCKED`
  notes, so `blocker_code` stays null and `blocked_gates` is shown as a count
  only. Owner interventions are also not observable from Harness and stay null.
- **Cleanup:** `CLEANUP_PENDING` tasks are reported with their age from
  `verified_at`. They are surfaced, never freed.
- **Harness DB access:** read with SQLite `mode=ro`; the content stays
  byte-identical. Because the DB is in WAL mode, a reader may leave the
  standard `-shm`/`-wal` sidecar files, which the service's next writer
  removes.
- **Narrated cycles:** cycles written by `cycle-record` (the automation's own
  account) are stored with `provenance=OPERATOR_ATTESTED`. They are EXCLUDED
  from L2/L3 and convergence, and the report shows them as
  `attested_cycles_excluded`. Older ledger rows are migrated to that label.
- **Service flag:** `harness_monitor_service.py --meta-ingest` runs the same
  ingest after a healthy progress write. It is opt-in and non-fatal: it never
  changes the service exit code or the Harness health flags.

## Status and owner decision
- CODE DONE: ingest, its tests and the opt-in service flag.
- NOT RUNTIME VERIFIED: the production ledger is empty.
- One real read-only smoke run wrote to a throwaway ledger only; that is not
  acceptance.
- **BLOCKED_OWNER:** automatic ingestion needs an owner-approved runtime
  change. After merging and deploying to the clean main checkout, add
  `--meta-ingest` to the `ExecStart` of
  `projectflow-harness-reconcile-staging.service`. That uses the same 5-minute
  timer, adds no schedule and changes no prompt, goal or gate. Until then it
  runs only by explicit invocation:
  `$PF scripts/harness_meta_monitor.py ingest`.
- Next, after activation:
  1. Collect at least 7 days of ingested cycles.
  2. Decide who records `defect-record` events.
  3. Optionally add structured `blocker_code` values to the gate policy (an
     owner policy change).
  4. Judge one experiment (for example ZGH-DISPATCH-GUARD-001) only from
     ledger evidence.
