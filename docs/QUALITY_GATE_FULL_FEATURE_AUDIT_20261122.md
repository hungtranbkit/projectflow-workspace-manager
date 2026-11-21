# ProjectFlow Quality Gate — Full feature and live-workflow audit (22 Nov 2026)

## Scope, evidence, and classification

Audit covers the **Quality Gate capability and the ProjectFlow worktree → agent → review → integration → merge → deploy workflow that it affects**. It is **not** a claim that all 66 service modules and every unrelated feature have been fully end-to-end accepted.

Evidence checked directly:
- GitHub integration branch `80496a120ff3ac5c8b8ecb81900609704884d405` (the only change after the deployed code commit is an operations document) vs current `main` `072b22bd5ab78a82e0b776ce390fe01492f2977b`.
- Dell live `workspace-manager.service`: `active`, `enabled`; actual `/proc/<MainPID>/cwd` is `/home/dell/workspace/projectflow-qualitygate-release-20261121` at source SHA `0858c0f4b5ce473063daba2ec37ac0e564b03a5f`.
- Dell local `/health`, `/api/tasks`, `/api/repositories` each HTTP 200. SQLite quick_check `ok`; 7 persisted tasks, 5 registered repos, 8 merge records. 139 GiB available on Dell.
- NEC `projectflow-qualitygate-ed-stage.service` `active` at loopback `127.0.0.1:18766`, selected **synthetic task #1** only, with a valid Ed25519 receipt as of inspection. Its expiry time is 22 Nov 2026 **07:26:01 +07:00**; no evidence of automated renewal.
- Dell focused QA suite **26 passed / 67 warnings** (isolated scratch configuration; no production DB mutation). Historical targeted merge/QA suite **90 passed** on tested code tree in NEC. Pytest collected **1,614 tests** from the Dell release, exit code 0; **the full 1,614 tests were not executed**.
- Live production GET probes: `/`, `/tasks`, `/tasks/4`, `/integrations`, `/api/tasks/4/decision`, `/api/tasks/4/observability`, `/api/tasks/4/reviews`, `/api/tasks/4/dependencies`, `/api/tasks/4/worktree`, `/api/tasks/4/parallel-safety` returned HTTP 200.
- **Live failure:** `GET /api/tasks/{id}/integration-readiness` returned **500** for task IDs 1,2,3,4,7 and **409** for IDs 5,6 (the 409 response included `fatal: not a git repository`). `GET /api/tasks/{id}/integration` returned 500 for 4 and 409 for 5,6. Read-only journal showed `FileNotFoundError` from a subprocess launched in a vanished worktree.
- Production SQLite `agent_workspaces`: **13 rows, 11 nonexistent worktree paths** (2 present); registered `repositories`: **2/5 missing directories**, the historical `vrepo-a` and `vrepo-b`. This may be expected after intentional cleanup, but the API must treat stale references as typed blockers rather than unhandled exceptions.
- Production SQLite task statuses `ACTIVE` for IDs 4,5,6; each decision API evaluates `DONE` with an underlying `MERGED` record and next action `CLOSE_TASK`. This is a workflow-finalization backlog, not proof that an agent is still coding.
- Production `agent_sessions`: 7 records: **6 EXITED, 1 FAILED, 0 RUNNING**. This is the Workspace Manager DB snapshot, not a claim about other unrelated Terminal MCP agents.

## Progress assessment

**Estimated end-to-end Quality Gate delivery: 59/100 acceptance points (~59%).**
The scoring is a **DoD engineering estimate, not an instrumented productivity metric**. It emphasizes production use and hard enforcement; green unit tests alone do not award all points.

| Acceptance capability | Weight | Earned | Reason |
| --- | ---: | ---: | --- |
| Reviewed task-specific policy coverage | 10 | 4 | Three pilot policy entries; signed attestation hardcodes only the monitor-report pilot |
| Reproducible pinned tests, lint, and machine evidence | 10 | 9 | Narrow QA tests PASS; full test suite remains unexecuted |
| Real no-network/read-only Docker QA | 10 | 8 | Pinned runtime proven on NEC; independent production signer not qualified |
| Ed25519 signatures and public-key verification | 10 | 8 | Real NEC canary and automated tests; single-use demo key, not managed production trust |
| Exact commit, expiry, tamper, fail-closed logic | 10 | 9 | SHA mismatch, missing, tamper and expired proofs tested |
| App-side PR/local merge gate implementation | 10 | 7 | Routes wired and tested; production selection list empty; integration readiness bugs |
| Operator UI, errors and diagnostics | 5 | 3 | Blocker messages in code; production readiness APIs have 500/409 failures, no QG acceptance dashboard |
| Dell code release, preservation and rollback | 10 | 9 | Running release, original live DB unchanged, valid off-host backup |
| Independent restricted signer and authenticated handoff | 10 | 1 | Mac QA signature experiment, **no independently delivered production receipt** |
| Native GitHub required merge protection | 5 | 0 | Current `main` does not contain QG; no verified required quality check/ruleset |
| Real-task pilot and block/pass KPIs | 5 | 0 | 0 production tasks opted in; 0 measured actual blocked merge events |
| Worktree/session/closure lifecycle robustness | 5 | 1 | 11/13 worktree paths stale; 2 missing repos; 5 readiness 500s; 3 tasks awaiting closure |
| **Total** | **100** | **59** | **Partially complete; not production-enforced** |

Code exists and core QA mechanics are largely implemented. **Actual mandatory protection of production merges remains 0 selected tasks.**

## Feature-by-feature outcomes

1. **Task creation and decision** — UI and GET work. But three merged tasks still stored ACTIVE until explicit CLOSE_TASK; avoid conflating stored status with decision DONE.
2. **Repository registration/discovery** — code present, 5 registered, 2 paths missing. Need orphan classification and UI repair, not destructive auto-delete.
3. **Agent worktree lifecycle** — code and real worktree tests exist, but 11/13 saved paths are missing; readiness must return deterministic `MISSING_WORKTREE`/stale state rather than throw.
4. **Agent session orchestration** — code/API present. In Workspace Manager DB, 6 EXITED, 1 FAILED, no RUNNING evidence; don't claim active agents from stale session records.
5. **Test / QA execution** — scoped runner and pinned policy available, historical 90 tests PASS, new 26 test PASS. 1,614 total tests collected but not full-run.
6. **Docker isolation** — real restricted test ran successfully; image SHA fixed, no network, read-only, nonroot, no Docker socket mount, bounded tmpfs.
7. **Ed25519 signer & verifier** — exact task/repo/SHA and 24h freshness; NEC pilot works. Independent private-key custody and secure handoff not proven on production.
8. **Review / blocker UX** — text blocker codes and guidance exist, but key `integration-readiness` endpoints crash on missing worktrees.
9. **GitHub PR / local merge gates** — code wired into `merge_gate_status`, HTTP merge and local integration. Production flag empty, so no production PR is stopped by this new signed gate. No verified mandatory native GitHub rule.
10. **Deploy / rollback** — Dell systemd active, persistent DB preserved, rollback unit/DB snapshot retained. Deployment contract is stale: `PROJECT.yaml` says `role: local-development-tool` and `deployment.production.enabled: false` while a production-like service is running.
11. **Observability and progress metrics** — GET observability responds, but no measured live signature pass rate, real blocked-merge counts, signer failure/latency, renewal alerts or adoption KPI evidenced.
12. **Disk/runtime stability** — Dell recovered from full disk to 139 GiB available, Terminal MCP healthy. Storage risk much reduced; SQLite integrity OK.

## Priority blockers and actionable DoD

### P0 / release blockers
- **Independent signer**: create a separately restricted QA identity and private-key location, run approved Docker test under confinement, sign exact commit only after PASS, send only receipt + public key through authorized authenticated channel. DoD: live end-to-end Ed25519 HTTP merge scenario (PASS, missing, stale, tampered), no key accessible to coding agent; strict flag `1` on a selected safe pilot.
- **Real task selection**: review and add a task/repo-specific policy for one actual production task (not arbitrary existing MESFlow/QA tasks). DoD: one real PR gate tested and block/pass evidence captured without bypass.
- **Merge enforcement outside app**: inspect existing protected GitHub `main` settings with an authorized owner; set required check/rules compatible with local CI. DoD: direct GitHub merge without signed QA cannot bypass policy. Current CI workflow triggers GitHub Actions on PRs and pushes to main; don't invoke it inadvertently.

### P1 / workflow correctness
- **Integration-readiness 500/409**: add existence/valid-repository checks before `git.head` or `check_integration`; map missing worktree, missing registered repo and closed/cancelled/merged task to stable blockers or terminal states. Add tests for all 7 observed tasks. DoD: 0 unhandled 500s and meaningful recoverable API responses.
- **Stale workspace reconciliation**: audit 13 stored workspaces, identify 11 missing paths, reconcile reference state without deleting live paths; repair or archive 2 nonexistent test repo references. DoD: owner-visible state and no stale directory crash.
- **Task close/reconciliation**: ensure 3 cases where DB ACTIVE and decision DONE are shown as `WAITING_CLOSE`, or closed by an explicit approved workflow. DoD: dashboard and automation do not report them as actively coding.

### P2 / quality of operations
- Execute a controlled wider suite from 1,614 collected tests in an isolated environment; publish pass/fail/skip/warnings without touching live data.
- Track QG-selected task count, signed receipt age and expiry, actual blocked merges, pass/failed attestation, QA runner duration and rejection reason.
- Reconcile README / PROJECT.yaml / systemd deployment claims; record rollback and key rotation policy.

## What NOT to claim

- `90/90 PASS` does not prove all 1,614 tests pass.
- An Ed25519 receipt on synthetic NEC task does not prove independent production signing.
- A protected GitHub `main` branch is not necessarily configured to require this Quality Gate; protection detail could not be inspected with the current connector.
- A healthy `/health` does not imply every integration/review API works; observed 500/409 is proof otherwise.
- A source deployment to Dell does not automatically enforce QA for any task; the production task selection environment variable is currently blank.
