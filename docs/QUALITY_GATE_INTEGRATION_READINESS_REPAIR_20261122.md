# Dell ProjectFlow integration-readiness P1 repair — 22 Nov 2026

## Release verdict

**DEPLOYED / VERIFIED:** Production `workspace-manager.service` on Dell Linux
now runs commit `69b7e4405d9275f71f4b46a3f7dd95a02a41ce24` from the
integration branch, using the existing production SQLite database and
workspace root. The previous clean release is preserved.

**Still NOT enforced:** `PROJECTFLOW_QUALITY_PILOT_TASK_IDS` remains empty and
`PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=0`. The independent signer /
GitHub-required merge protection have not been qualified.

## Observed incident and root cause

The live production DB retains agent-worktree rows after temporary worktrees
were cleaned up. The previous `ReviewFixOrchestratorService.integration_readiness`
called `git.head(worktree_path)` without checking if its cwd existed. The
subprocess then raised `FileNotFoundError` (HTTP 500), while other stale Git
paths returned HTTP 409.

Prior live probe: `GET /api/tasks/{id}/integration-readiness`:
- Task IDs **1,2,3,4,7: HTTP 500**
- Task IDs **5,6: HTTP 409**

Audit facts:
- 13 persisted worktree records, 11 paths missing.
- 5 registered repositories, of which two discontinued synthetic repositories
  (`vrepo-a`, `vrepo-b`) do not exist. Those two are already `enabled=0`
  and link only to cancelled task #7.
- Three merged tasks had persisted `status='ACTIVE'`, but the computed
  TaskDecision status is DONE. Task #5 and #6 already had `closed_at`
  timestamps; task #4 had not yet been closed.
- The stored task status is not the source of truth for the UI:
  `task_card_view` already renders the computed TaskDecision status.

## Actual code change and safety

`app/services/review_fix_orchestrator.py` now:

- Returns structured `BLOCKED / TASK_CANCELLED` for cancelled tasks.
- Returns `BLOCKED / ALREADY_MERGED` if all required merge records
  are already MERGED; does not attempt another Git merge.
- Returns typed blockers for unavailable worktree/repository paths:
  `REPOSITORY_MISSING`, `WORKTREE_MISSING`,
  `WORKTREE_INVALID`, or `INTEGRATION_UNAVAILABLE`.
- Checks that the canonical repository is valid before running Git,
  validates worktree containment, and converts expected Git/OS failures to
  blocking outcomes rather than unhandled API errors.
- Preserves normal code review/security/finding/human-approval gates when
  the workspace is usable. Missing evidence never silently becomes READY.
- Preserves the historical DB rows and existing worktree files. **No
  deletion or synthetic recreation of worktrees** was required.

## Verification evidence

### Code and isolation tests

A new regression file
`tests/test_integration_readiness_orphans.py` covers deleted worktree,
missing canonical repository, invalid `.git` metadata, cancelled tasks,
already-merged tasks, and an ordinary legitimate reviewed worktree.

- Focused integration + review test run: **35 passed, 399 warnings, 53.57s**.
- Expanded merge/release test run: **63 passed, 2 failed, 727 warnings,
  116.83s**. The two failures were sandbox RUNNING/HEALTHY state checks:
  `test_cross_repo_integration.py::test_backend_and_client_test_together_without_either_merging_to_main`
  and
  `test_merge_reconciliation.py::test_done_ui_shows_verification_sandbox_action_when_supported`.
- Critically, **the same two sandbox tests failed on the PRE-CHANGE
  production release** under the same Dell Docker environment. This confirms
  they are existing environment/baseline failures, not newly introduced by
  the integration-readiness patch. They remain open and must not be counted
  as PASS.
- New release smoke using a SQLite BACKUP of production under `/dev/shm`:
  **10/10 GET checks HTTP 200** (Health, Tasks, Repositories and readiness
  for task IDs 1–7); no production DB or files were mutated by the test.

### Real production cutover

Before cutover:
- Current unit drop-in and consistent SQLite backup archived at
  `/home/dell/workspace/projectflow-qualitygate-release-20261122/rollback-before-20261122/`.
- Backup `workspace-manager.db`: SQLite `quick_check=ok`.
- Previous running code:
  `/home/dell/workspace/projectflow-qualitygate-release-20261121`,
  SHA `0858c0f4b5ce473063daba2ec37ac0e564b03a5f`.

After cutover:
- Edited only
  `~/.config/systemd/user/workspace-manager.service.d/90-qualitygate-release.conf`
  to point at new release folder, then `daemon-reload` and restarted the
  user unit; restart exit code 0.
- `workspace-manager.service` = **active**.
- Process `/proc/<MainPID>/cwd` = new release checkout.
- `/health` = `{"status":"ok","auth_mode":"none"}`.
- `/api/tasks/{id}/integration-readiness` = **200 for all 7/7 tasks**.
- `/api/tasks/{id}/integration` = **200 for all 7/7 tasks**.
- The JSON response correctly indicates `BLOCKED` with
  `TASK_CANCELLED` for IDs 1,2,3,7, and `ALREADY_MERGED` for
  IDs 4,5,6; **200 is a successful read, not approval to integrate**.
- SQLite quick check = `ok`; 7 tasks and 5 repositories preserved.
- Free disk space at final verification: approximately **138 GiB**.

Task #4 was verified: computed `DONE`, one required MERGED record,
no sandbox and no live agent sessions, `closed_at` previously null.
`POST /api/tasks/4/close` returned **HTTP 303** and persisted
`closed_at`. Tasks #5 and #6 were already closed. The historical
`status='ACTIVE'` field remains as designed, and computed state remains
DONE. No raw status rewrite was attempted.

## Rollback

To roll back CODE without discarding newer live data, restore the saved
`service-dropin.conf` over
`~/.config/systemd/user/workspace-manager.service.d/90-qualitygate-release.conf`,
then run `systemctl --user daemon-reload` and
`systemctl --user restart workspace-manager.service`. Verify localhost
`/health` and process CWD. Do **not** replace the production DB with
its older backup unless an actual data issue has been investigated and
post-cutover writes reconciled.

## What still needs work

1. **Quality Gate production enforcement remains OFF.** An independently
   protected Ed25519 signing principal and authenticated signed receipt
   handoff are not yet demonstrated, and reviewed policy only covers the
   monitor-report pilot. Do not set the task flag without that evidence.
2. **GitHub direct merges** are not governed by the application-only gate.
   `main` is separate and CI triggers GitHub Actions on PR/main pushes.
3. The two **pre-existing Docker sandbox failures** should be diagnosed
   separately, with a local-image/engine/runtime baseline check.
4. Historical worktree rows should be displayed/reconciled as MISSING,
   not deleted indiscriminately; no real working directories were
   recreated or removed by this repair.

Release acceptance: **integration-readiness P1 FIXED**, production
Quality Gate's mandatory signed-merge protection **NOT COMPLETE**.


## Same-day Docker network remediation — completed after first audit

The two previously failing Docker sandbox tests were investigated further.
In the disposable pytest SQLite record, both sandbox provisioning operations
had `COMPOSE_UP_FAILED` and the Docker daemon reported:

```
all predefined address pools have been fully subnetted
```

This failure occurred on the OLD release as well, confirming an infrastructure
baseline failure rather than a regression caused by the Quality Gate change.

Read-only Docker audit showed 33 networks, including ten with no attached
containers. Five explicit **unused CI/test networks** (zero attached
containers each) were removed; no Docker volumes, running containers, active
production networks or daemon process were changed:

- `cdtm-issue109-test`
- `mesflow-app-chatbot-ci_default`
- `mesflow-ci-stabilization-41359202744_default`
- `mesflow-test_default`
- `repoport-prod-smoke_default`

A new isolated test network could then be created successfully with allocated
subnet `172.20.0.0/16`; the temporary proof network was removed afterward.

**Retest evidence:**
- Two previously failing tests: **2/2 PASS**, 34 warnings, 6.98 seconds.
- Full targeted release/merge/integration/attestation/orphan-regression suite:
  **65/65 PASS**, 727 warnings, 123.08 seconds; exit code 0.
- Test environment: disposable Git/SQLite fixtures on Dell; no production
  repository or database modified.
- Remaining test resources: **0 `wm-*` containers and 0 `wm-*` networks**.
- Live Dell `workspace-manager.service` and `/health` remain active/OK.

The original 63 PASS / 2 FAIL result remains in the chronology above, but
the **final same-day qualification is 65 PASS / 0 FAIL** after correcting
the Docker host's exhausted network-address pool. This does not prove the
unexecuted remainder of the repository-wide 1,614 tests passes.

## Progress adjustment

Relative to the 59/100 acceptance-point audit snapshot, the safe handling
of 7/7 real task readiness APIs, lifecycle/closure repair, and baseline
Docker recovery justify an estimated **64/100** end-to-end readiness
(acceptance-rubric estimate, not a measured productivity improvement).

Critical remaining release blockers are unchanged:
- Production signed QA policy is still opt-out; 0 real protected tasks.
- No independently restricted production Ed25519 signing identity and
  authenticated receipt delivery proven.
- GitHub `main` is not governed by a required Quality Gate check.
