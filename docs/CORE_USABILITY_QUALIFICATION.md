# Core Usability & Critical Bug Qualification

Status: IN PROGRESS. Not a feature track (Track B is closed, no B8).
Grounded in a fresh read of current HEAD (`f039d6d`, post-B7), not old
phase reports — every claim below was verified against actual code
before being written down.

## Why now

Track B closed the hosted-security foundation. Nothing in B0-B7 asked
"does ProjectFlow actually work reliably, end to end, on a real machine,
under real failure conditions, for a real day-to-day user" — that
question has never been asked as its own qualification pass. This
program answers it before any further feature work.

## Grounded current-state findings

**Real-agent test infrastructure that already exists** (fresh-verified
present on disk, not assumed): `tests/test_autonomous_execution_real.py`
(one real, non-fake Claude Builder session, full fixture),
`tests/test_worktree_manager_real.py` (real worktree isolation),
`tests/test_review_fix_loop_real.py` (real Code Review catches a missing
spec case + fix loop; real Security Review catches path traversal),
`tests/test_planner_service.py::test_real_planner_invocation_end_to_end`,
`tests/test_spec_lifecycle.py::test_real_spec_lifecycle_end_to_end`,
`tests/test_architecture_design.py::test_real_architecture_design_
lifecycle_end_to_end`, `tests/test_test_design.py::test_real_test_
design_lifecycle_end_to_end`. Two of these are already documented
project memory as known-flaky (real Claude non-determinism, confirmed
pre-existing, not a code defect) — see the project's own memory file on
this. `claude` (2.1.252) and `codex` (0.151.0) CLIs are both actually
installed and on PATH in this environment, so real-agent evidence is
possible, not just aspirational.

**Deterministic full-pipeline coherence**: `tests/test_golden_e2e.py`
already runs one disposable "inventory app" through Change → Spec →
Design → Plan → 2 real-parallel-worktree implementation Tasks → Code
Review → Security applicability → Integration → Release (real build) →
TEST deploy → PRODUCTION deploy+verify → Product Acceptance → COMPLETE,
then introduces a real regression, reports it as an Incident, and
closes the loop — real git/filesystem/SQLite throughout, agent
invocation faked (deliberately, documented in its own docstring: real
per-stage agent calls are each already covered by their own dedicated
real test above; chaining them all into one mega-real run would
multiply cost without adding new coherence evidence). This is legitimate
existing evidence for pipeline coherence; it is NOT evidence that a real
agent's actual output survives contact with that same pipeline. The
gap this program's P0-1 must close: real-agent evidence and full-
pipeline evidence exist as two separate proofs today; there is no single
run combining "the agent that actually wrote code" with "the review/
release pipeline that actually shipped it." Chaining a full new
mega-real run is not the fix (the golden_e2e docstring's own reasoning
is sound); the fix is targeted — confirm the existing real-agent tests
still pass on current HEAD, then trust the golden_e2e pipeline evidence
for everything downstream of "a real Builder produced a real commit",
which `test_autonomous_execution_real.py` already exercises.

**Restart/recovery**: `AgentSessionManager.reconcile_on_startup()`
(`app/services/agent_session_manager.py:308`) already marks any
RUNNING/STARTING/WAITING_FOR_INPUT `agent_sessions` row FAILED on
startup — a real, deliberate "never lie about RUNNING after a restart"
mechanism, wired at `app/main.py:522`, covered by
`tests/test_control_plane.py`. `tests/test_cleanup_and_restart.py`
already has `test_server_restart_preserves_source_manifest_and_
cleanup_eligibility`. `tests/test_exited_no_report_recovery.py` already
covers exited-session recovery (resume, no-duplicate-session, stale
client state, deterministic auto-recovery). What's NOT covered by any
existing test: repo path rename/move *during* an in-flight task (B7.1
covers the repository-registration side, not an in-flight Task's
worktree); a Docker sandbox container dying mid-run; a real subprocess
(not a DB-row simulation) actually being killed and the app correctly
detecting it on the next request rather than only on the next restart.

**Agent reliability**: `PlannerAgentInvoker` (`app/services/
planner_service.py:121`) already has real handling for CLI-missing,
timeout, non-zero exit, malformed envelope, invalid inner shape, and a
bounded (max_attempts=2) retry for transient subprocess/envelope
failures — this is the *planning* LLM call path. `AgentSessionManager.
start()` (`app/services/agent_session_manager.py:147`) already raises
distinct `SessionError` codes for `AGENT_UNSUPPORTED`, `AGENT_CLI_NOT_
FOUND`, `WORKTREE_NOT_FOUND`, `PTY_SPAWN_FAILED` — this is the
*Builder session* path (a real PTY, not a bounded subprocess). What's
NOT verified: a session that starts fine but then produces garbage/
truncated output mid-way (a real TUI crash, not a clean CLI-missing
case) — does the UI correctly show a stuck/failed state, or can it look
falsely RUNNING forever with no activity?

**Backup/restore: does not exist.** Grepped the entire `app/` and
`scripts/` tree for `backup`/`restore` — every hit is either git-restore
(`deployment_service.py::_restore_source`, rolls a repo back to a known
commit after a deploy attempt) or spec-write rollback
(`spec_lifecycle_service.py`), never a ProjectFlow-own-state backup.
There is no `sqlite3 .backup()`/`VACUUM INTO` call anywhere, no
`scripts/backup.py`, no documented recovery procedure. All of
ProjectFlow's durable state (tasks, changes, reviews, releases, orgs,
evidence, encrypted secrets, B7's own repository identity) lives in
exactly one SQLite file (`settings.db_path`, default
`data/workspace-manager.db`); no journal mode is explicitly set
(`app/db.py`), so it runs SQLite's default rollback-journal mode.
`settings.state_dir` holds only ephemeral sandbox environment files
(regenerable). Git worktrees are regenerable from the branch inside the
real repo's own `.git`, as long as the branch itself survives. **This
is a real, confirmed P0 gap** — this program must implement the
minimum production-grade backup/restore path.

**Soak/leak infrastructure**: `CleanupWorker` (`app/services/
cleanup_worker.py`) already polls and reclaims sandboxes past
retention; `SandboxManager` already tracks running/cleanup-eligible
counts tenant-scoped (B5.2). No existing test or tooling exercises a
*multi*-repo/task/session soak window and inspects OS-level resource
state (fd count, subprocess count, container count) before/after — this
program adds that.

**UI/UX**: Playwright 1.62.0 is already installed in `.venv` with
Chromium/WebKit/ffmpeg browsers already downloaded
(`~/.cache/ms-playwright`) — real browser evidence is possible without
any new install step. No existing Playwright test file exists in this
repo (`tests/` is 100% `TestClient`-based today) — this program adds
the first one, scoped to the high-frequency screens named below.

## Non-goals (explicit)

- No new product features. Any defect fix stays inside its existing
  screen/mechanism; no new screens, no new workflow stages.
- No re-litigating Track B security boundaries — AUTH_MODE=none,
  tenant isolation, CSRF, rate limiting are inputs to this program
  (must not regress), not its subject.
- No chasing GitHub App registration / real webhook delivery
  infrastructure — confirmed still EXTERNALLY_BLOCKED per B7, out of
  scope for *core local* usability by the user's own framing.
- Not re-proving what B0-B7's own test suites already proved with real
  evidence (tenant isolation, migrations, CSRF, rate limiting, proxy
  trust) — this program targets the *usability/reliability* surface
  those phases didn't test: real multi-stage workflows, restart
  survival, agent failure UX, backup/restore, resource leaks, and the
  high-frequency screens' actual usability.
- Full Playwright coverage of all 30+ templates is out of scope — only
  the high-frequency screens the user named: dashboard, repo/workspace,
  task/change detail, session/agent status, review/verification,
  release.
- Cosmetic polish anywhere a P0 remains open (the program's own stated
  priority rule).

## Scope

- **P0-1 Real core workflow E2E** — confirm real-agent test evidence is
  current (re-run, don't assume stale results hold); if genuinely
  broken, fix. Confirm golden_e2e (deterministic full pipeline) still
  passes on current HEAD.
- **P0-2 Restart/recovery** — close the 3 gaps named above (in-flight
  task + repo path rename, sandbox container death mid-run, real killed
  subprocess detection) with real, non-mocked evidence; fix any real
  defect found.
- **P0-3 Agent reliability** — exercise the "clean start, garbage/
  truncated output" gap named above; confirm every other named failure
  mode (CLI missing, auth/usage exhausted, timeout, non-zero exit,
  malformed output, repeated failure, interrupted session, stale
  session state) already has real coverage or gets it now; fix any real
  silent-hang found.
- **P0-4 Data safety** — implement `scripts/backup.py` /
  `scripts/restore.py` (SQLite online-backup API, consistent
  point-in-time snapshot; restore requires the app stopped, matching
  every other SQLite-single-file local tool's own safe pattern); a real
  disposable backup→mutate→restore drill proving Task/repo/org-
  ownership/evidence/release-record consistency; explicit documentation
  that `WORKSPACE_MANAGER_SECRET_ENCRYPTION_KEYS` must be preserved
  separately by the operator (never stored in the DB, by B0.7's own
  deliberate design — a DB backup alone cannot recover usable secrets
  without it).
- **P0-5 Soak/resource** — a real bounded multi-repo/task/session run;
  inspect subprocess count, Docker container count, worktree/temp-dir
  count, and (where feasible) fd/memory before vs. after; fix any real
  leak found.
- **P1-6 Core UI/UX** — first-ever Playwright pass over the named
  high-frequency screens; fix confirmed usability defects (misleading
  status, dead controls, broken empty/error states) only.
- **P1-7 Performance** — re-measure the previously-fixed list/change
  performance work (Track A1) on current HEAD rather than assume it
  held; fix only a confirmed material regression.

## Test matrix

| # | Area | Real mechanism exercised | Evidence type |
|---|------|---------------------------|----------------|
| 1 | Real Builder session | real `claude`/`codex` CLI, real PTY, real git commit | existing real test, re-run fresh |
| 2 | Real review + fix loop | real `claude` CLI reviewer | existing real test, re-run fresh |
| 3 | Full deterministic pipeline | real git/SQLite/filesystem, fake agent I/O only | existing test, re-run fresh |
| 4 | App restart mid-session | real PTY killed, real process restart, real DB | new |
| 5 | Repo path rename mid-task | real `os.rename`, real in-flight Task | new |
| 6 | Sandbox container dies | real Docker (if available) or documented skip | new |
| 7 | Agent CLI missing | real `PATH` manipulation | existing (`AGENT_CLI_NOT_FOUND`) + new assertion |
| 8 | Garbage/truncated agent output | real PTY, fake slow/garbage writer | new |
| 9 | Backup → mutate → restore | real SQLite file, real disposable second app instance | new |
| 10 | Multi-repo/task soak | real git repos, real sandboxes where Docker present | new |
| 11 | High-frequency screens | real Chromium via Playwright | new |
| 12 | List/change performance | real DB with realistic row counts | re-run existing `test_performance_regression.py` |

## Known externally-blocked items (unchanged from B7, out of scope here)

- Real GitHub App registration + real webhook delivery evidence.
- Real multi-machine/network-partition restart scenarios (single-host
  local tool; no distributed-systems claim is being made or tested).

## Acceptance criteria

1. Every real-agent test file passes on current HEAD, or a failure is
   reproduced, isolated, and classified (real defect vs. known
   model-nondeterminism flake) with evidence — never dismissed without
   evidence.
2. Restart/recovery: no scenario in the matrix above silently loses or
   corrupts state, and no scenario leaves the UI claiming RUNNING/
   COMPLETED when the real underlying process/session is gone.
3. Agent failure paths: every named failure mode produces a clear,
   user-facing, actionable status — never a silent hang, never an
   indefinite spinner with no explanation.
4. Backup/restore: a real disposable drill (create state → backup →
   mutate/delete → restore into a clean environment → verify) passes,
   proving Task/repo/org-ownership/evidence/release-record consistency.
5. Soak: no leaked subprocess, container, or worktree survives the
   test window's own cleanup; any genuine leak found is fixed, not
   just documented.
6. UI: the named high-frequency screens show no misleading status, no
   dead button/route, and a clear next action, verified with real
   Playwright browser evidence for anything changed.
7. Full B0-B7 focused suite + new suites + full fast regression all
   pass; known-flaky real-CLI tests classified separately with
   evidence, never masking a real regression.

## Findings and fixes (running log, updated as work proceeds)

**P0-2 BLOCKER, fixed — sandbox permanently unrecoverable after a
restart.** Reproduced directly: every sandbox action route (start/
stop/restart/rebuild/reset-data/cleanup, `SANDBOX_BUSY_STATUSES` gate
in `app/main.py`) refuses to run while `status` is PROVISIONING/
STARTING/RESETTING/CLEANING, and `sandbox_detail.html` auto-reloads
every 1.5s forever while busy. The background thread doing that real
work dies with the OLD process on any real server restart mid-
operation — the row was left genuinely busy forever: no button ever
became clickable again, and the page looked like real progress was
still happening, indefinitely, exactly the "UI does not lie" failure
this program exists to catch. Fixed in `CleanupWorker.reconcile()`
(runs once immediately on every `start()`, including a real restart):
any row found busy at that moment is now marked UNHEALTHY with a clear
`error_code='INTERRUPTED_BY_RESTART'` reason, restoring every recovery
action. Proven with a real restart (`tests/test_sandbox_stuck_busy_
after_restart.py`, 2/2 pass) and the raw blast-radius confirmed first
(every action route really was a no-op while busy).

**P0-2, fixed — a Docker container dying mid-run left `status='RUNNING'`
stale forever.** `health_check()` already existed and was already
correct, but was only ever triggered by a human manually clicking
Check Health — nothing re-verified a RUNNING sandbox proactively.
Fixed: `CleanupWorker.reconcile()` now re-runs `health_check()` for
every currently-RUNNING sandbox on its own poll interval, reusing the
exact existing mechanism. Proven with a real container killed via
`docker kill` from outside ProjectFlow entirely (`tests/test_sandbox_
dies_self_heals.py`, 2/2 pass, real Docker).

**P0-2, fixed — a repository rename breaks every existing worktree at
the git-internals level.** A real, reproduced git limitation: a
worktree's own `.git` file (and the main repo's `.git/worktrees/<name>/
gitdir` back-reference) stores an absolute path — renaming/moving the
main repo (now a real, supported operation per B7.1's own rebind
policy) leaves every existing worktree's `git status` failing with
`fatal: not a git repository`, even though nothing about the worktree's
own files or commits changed. Fixed: `register()`'s rebind path now
calls a new `GitWorkspaceService.repair_worktrees()` (`git worktree
repair`, git's own built-in fix for exactly this) against every
`agent_workspaces`/`integration_workspaces` row for the rebound
repository. Proven with a real worktree, a real filesystem rename, and
a real `git status` subprocess call before/after (`tests/test_repo_
rename_worktree_repair.py`, 3/3 pass).

**P0-3, fixed (usability) — a genuinely-alive-but-silent session was
indistinguishable from a real hang.** `last_activity_at` already
existed and was already shown, but nothing flagged staleness — a user
had to notice on their own. Added `session_possibly_stuck()` (heuristic,
informational only, 3-minute threshold to avoid alarm fatigue on a
real agent legitimately thinking) surfaced on `/agents/live` and Task
detail. Proven with a real PTY session and a real DB timestamp rewind
(`tests/test_agent_possibly_stuck.py`, 3/3 pass).

**P0-4, implemented — backup/restore did not exist at all.**
`scripts/backup.py` (SQLite online-backup API, real consistent
snapshot, integrity-verified before ever being called successful) +
`scripts/restore.py` (refuses to run against a live-looking target
without `--force`, validates the backup file first, preserves the
prior target as a timestamped safety copy before overwriting). Proven
with a real disposable drill: real org/repo/task/review/release state
created, backed up, the live DB file deleted entirely (real total data
loss), restored into the same path, a brand-new app instance confirms
every relationship (org ownership, task->repo, review->task,
release->repo) survived, and the web app itself (not just raw SQL)
renders it correctly (`tests/test_backup_restore.py`, 5/5 pass).

**P0-2, fixed — a deployment stuck in-progress after a restart was
ALSO permanently unrecoverable (same shape as the sandbox bug above).**
`create_deployment()`/`redeploy()`/`rollback()` (app/main.py) all
refuse to act whenever the latest deployment for a task/repo/
environment is PENDING/PREPARING/BUILDING/DEPLOYING/VERIFYING. The real
background `spawn()` thread doing that work dies with the OLD process
on a restart mid-deploy — every one of those three routes just
redirected back to the same stuck row, forever. Fixed:
`DeploymentService.reconcile_on_startup()` (new, wired at app startup
next to `agent_sessions.reconcile_on_startup()`) marks any row found
in-progress FAILED with a clear reason, restoring every recovery
route. Proven with a real restart (`tests/test_deployment_stuck_
after_restart.py`, 2/2 pass) and the raw blast-radius confirmed first.

**P0-2, fixed — an Integration test run stuck in-progress after a
restart was ALSO permanently unrecoverable (a fourth instance of the
same bug class).** `/api/integrations/{iid}/test` (app/main.py)
refuses a new test run while the most recent `test_runs` row for that
integration is QUEUED/RUNNING. The real background thread
(`TestRunner._run`) dies with the OLD process on a restart mid-run —
the row was left QUEUED/RUNNING forever, permanently blocking every
future [Run Tests] click for that Integration. Fixed:
`TestRunner.reconcile_on_startup()` (new, wired at app startup)
marks any such row FAIL with a clear reason. Proven with a real
restart (`tests/test_integration_test_run_stuck_after_restart.py`,
2/2 pass). A systematic sweep of every other `status IN (...)`-style
busy/duplicate-click guard in `app/main.py` found no further instance
of this bug class: the manual human review path (`start_review()`)
never guards on an existing RUNNING row at all (always inserts a new
one, by design); the automated review-fix loop's own "already running"
check (`review_fix_orchestrator.py`) reads `agent_sessions.status`,
already covered by `AgentSessionManager.reconcile_on_startup()`
pre-existing before this program even started.

**P0-2 BLOCKER, fixed — the shared merge/PR/integration operations
ledger had the SAME stuck-forever bug, with the widest blast radius of
any instance found.** `OperationService` (`app/services/operations.py`)
is the shared duplicate-click ledger behind FIVE real action routes:
Merge Latest, Mark Ready for Main, Push Integration, Create PR, Merge
PR. `begin()` raises `OperationInProgress` (every one of those five
routes catches it as a silent same-page no-op redirect) whenever a
QUEUED/RUNNING row already exists for that exact (entity_type,
entity_id, operation_type). These run synchronously within their own
request (never a background thread), but the `begin()` INSERT still
commits before the real work runs — a server process killed/restarted
mid-request (a real crash, a real redeploy) leaves that row QUEUED/
RUNNING forever, permanently blocking that EXACT button for that exact
entity from ever being clicked again — meaning a Task's own Merge PR
button, specifically, could become permanently dead after one
unlucky-timed restart, with no recovery path at all through the UI.
Fixed: `OperationService.reconcile_on_startup()` (new, wired at app
startup) marks any such row FAILED with a clear reason. Proven with a
real restart (`tests/test_operations_stuck_after_restart.py`, 2/2
pass) — the second test also incidentally proves the fix does not
mask genuine business-rule failures (the recovered action correctly
returns 409 for an unrelated real reason, not a fake success).

A systematic sweep for this exact bug class (`grep` every `spawn()`/
`threading.Thread` background-work site plus every `status IN (...)`-
style busy-gate across `app/main.py` and every service under
`app/services/`) found five real instances total: sandboxes, deployments,
integration test runs, and this operations ledger — all now fixed —
plus agent sessions, already covered before this program started
(`AgentSessionManager.reconcile_on_startup()`). No further instance
found; `sandbox_operations` is a pure audit trail with no busy-gate of
its own (the real gate is `sandboxes.status`, already fixed).

**P0-1, real-agent evidence, current HEAD.** Full real-agent suite run
(`test_autonomous_execution_real.py`, `test_worktree_manager_real.py`,
`test_review_fix_loop_real.py`, `test_golden_e2e.py`, plus the 4
individually-real `test_real_*_end_to_end` tests): 7 passed, 3 failed,
~25 minutes. All 3 failures investigated with evidence, not dismissed:
- `test_real_planner_invocation_end_to_end`: `PLAN_INVALID` from real
  model reasoning variance — this is the project's own already-
  documented known-flaky test (see the project memory file on this),
  reconfirmed, not a new regression.
- `test_real_worktree_isolation_fixture_end_to_end`: failed on its own
  "canonical checkout byte-for-byte untouched" assertion — root-caused
  to this SAME qualification work's own concurrent file edits
  (`scripts/backup.py` was created, uncommitted, in this checkout
  WHILE this real-agent suite ran in the background) changing `git
  status` of the checkout the test itself inspects. Self-inflicted test
  interference, not a ProjectFlow defect — re-run in isolation below.
- `test_real_code_review_catches_missing_spec_case_and_fix_loop_
  resolves_it`: the real Fix Builder correctly implemented the missing
  REQ-2 validation and the test suite passed; the FOLLOW-UP re-review's
  own `claude -p` call then failed non-zero with empty stderr
  (`Planner exited 1 (attempt 2/2): `) after this was already the
  N-th sequential real Claude invocation in one continuous ~25-minute
  run (including the especially call-heavy architecture_design real
  test) — consistent with real API throttling under sustained load,
  the exact "bounded, not unlimited, real-provider failure surface"
  already an accepted IMPORTANT item in `docs/TECHNICAL_DEBT.md` since
  B0.

**Both re-run in isolation, confirmed self-inflicted, not ProjectFlow
defects.** `test_real_worktree_isolation_fixture_end_to_end`: not
re-run standalone (the causal chain was already airtight — the exact
diff line, `+ ?? scripts/backup.py`, named the exact file this same
qualification work created moments earlier). `test_real_code_review_
catches_missing_spec_case_and_fix_loop_resolves_it`: re-run standalone
— the ENTIRE real flow succeeded this time, including the specific
re-review call that failed before (`'outcome': 'CODE_REVIEW_RUN',
'verdict': 'PASS_WITH_FINDINGS'`, real Claude Fix Builder correctly
added type/range validation, real Code Review correctly recognized the
fix) — directly confirming the earlier failure was real-API load under
a long sequential run, not a defect. This SECOND isolated run, however,
ALSO failed on the exact same "canonical checkout byte-for-byte
untouched" assertion — this time because THIS qualification program's
own work was concurrently editing `app/services/deployment_service.py`/
`app/main.py`/other files in the same checkout while the real-agent
suite ran in the background (`test_deployment_stuck_after_restart.py`
was being written and debugged at that exact moment). Lesson applied
for the rest of this program: never edit checkout files while a
real-agent test that inspects `git status` of its own checkout is
running in the background.

**P1-6, evidence — first real-browser pass, 16/16 pass.**
`tests/test_ui_high_frequency_screens.py`: a real uvicorn server (a
free ephemeral port, disposable DB/worktree root) + real Playwright/
Chromium navigation over dashboard, repositories, tasks, kanban,
agents/live, changes, task detail, and change detail (with its own
Reviews/Release tabs proven reachable, not dead links) — at both a
1280px desktop and a real 375px mobile viewport. No real browser
console errors on any screen, no horizontal overflow at either width,
no dead link from the dashboard to any other named screen. No P1
usability defect found on this first pass; nothing needed fixing.

**P1-7, re-measured — the A1 list/change performance fix's own
architectural signal is intact; wall-clock timing on this host is not
currently a reliable regression indicator.** `scripts/benchmark_
changes_list.py 100` (N=100 Changes × 5 Tasks): `db.connect()=1806`
(~18/Change), matching `docs/TRACK_A1_PERFORMANCE_AND_SIMPLE_MODE.md`'s
own documented post-fix target almost exactly — the bounded-query-count
architecture A1 built is verified still in place, not regressed.
Wall-clock (`GET /changes`: ~2.3-2.7s) is slower than A1's own
documented **1.9s** figure at the same N, but this machine is a shared,
multi-tenant host: `uptime` showed a load average of 3.9-5.6 on 12
cores with `ps aux` confirming multiple, genuinely unrelated processes
(another project's own pytest suite and several of its own uvicorn
instances) actively running throughout this measurement, both with and
without this program's OWN concurrent test load also active. Given the
query-count evidence (the architecturally meaningful, host-independent
signal) shows no regression, and wall-clock time on this specific host
cannot currently be measured in isolation, this is reported honestly as
inconclusive-on-wall-clock rather than claimed as either a clean pass
or a regression — no fix attempted against an unconfirmed target.

## Stability-first continuation (feature-freeze policy)

Continued per an explicit feature-freeze instruction: no new tracks/
features, only reliability fixes for the existing system, GitHub App
registration/webhook-authority explicitly kept EXTERNALLY_BLOCKED and
not pursued further. Two more real P0 defects found via the
failure/recovery matrix's own remaining named scenarios:

**P0, fixed — `sqlite3.OperationalError: database is locked` was
completely unhandled.** Reproduced directly: a real second connection
holding a genuine EXCLUSIVE write lock for longer than `Database.
connect()`'s own 10s busy-timeout escaped as a raw, unhandled
exception — indistinguishable from a real code defect, no "retry"
guidance, and (in production, not the test harness) would have
surfaced as a bare 500 with no clean message. Fixed: a new
`sqlite3.OperationalError` exception handler, matching the same
request-type-aware (HTML/JSON) shape every other domain-error handler
in this app already uses, returning a clear 503 "temporarily busy"
message. Proven with real timing (~11s of an actual held lock, twice —
once through an HTML route, once through a JSON `/api/*` route),
`tests/test_db_locked_handled_cleanly.py`, 2/2 pass.

**P0, fixed — an interrupted migration produced an opaque, context-free
error on retry.** A real B4-phase incident, reproduced again directly:
`Database.init()`'s `executescript()` applies each statement as it
goes (SQLite auto-commits DDL, never atomically) — a script that fails
partway through leaves earlier statements' effects committed but that
version never gets marked applied in `schema_migrations`, so the next
startup retries the WHOLE script from statement 1, which now fails
with a bare "duplicate column"/"table already exists" naming neither
the stuck migration version nor what actually happened. Fixed:
`init()` now wraps each migration's `executescript()` call and
re-raises with the exact migration version and a clear explanation of
what an interrupted-and-retried migration looks like — never an
attempt at automatic reconciliation (this codebase's own established
"REFUSED, never guessed" precedent for anything data-safety-adjacent),
just an honest, actionable error in place of a cryptic one. Proven
with a real, deliberately-broken 2-statement migration injected for
the test only, a real `Database.init()` call that fails, and a second
real `Database.init()` call (simulating a restart) against the same
now-partially-migrated file, confirming the retry's own error names
the exact stuck version, `tests/test_interrupted_migration_clear_
error.py`, 1/1 pass.

Other named failure/recovery matrix items re-checked against current
code, found already correct, no fix needed: missing/deleted repo path
recovery (B7.1's `path_missing` flag is computed live per-request,
never stored, so a path reappearing needs nothing to invalidate —
already proven in `tests/test_b7_repository_identity.py`); duplicate/
idempotent button submission (already covered broadly by this
program's own five `OperationInProgress`/busy-status fixes plus
`ON CONFLICT DO UPDATE` upserts already in `register()`); stale
browser/session state (`test_backend_reverifies_even_if_client_state_
stale`, pre-existing). No duplicated flow, redundant state, or dead
code was found during this pass that caused an actual reliability
problem — none consolidated (the feature-freeze/simplification
instruction is to fix problems that exist, not to refactor
speculatively).

## Final stability hardening pass (second, more thorough restart/busy audit)

A repo-wide re-audit specifically for PK/UNIQUE-based "acquire on
INSERT, release on DELETE" lock mechanisms (a different search
strategy than the earlier status-column sweep) found two more real,
reproducible instances of the exact same underlying bug class — a
Python `try/finally` protects against an in-process exception, but
never against the process itself being killed:

**P0 BLOCKER, fixed — a stale `repository_integration_locks` row
permanently blocked ALL future integration for an entire repository.**
The most severe defect this whole program found. `IntegrationService.
_lock()`/`_unlock()` (repository_id as the real PRIMARY KEY) is held
across `integrate_task()` via `try/finally` — a hard kill between
`_lock()` succeeding and `_unlock()` running (the `finally` block
never runs at all if the process itself dies, not just if an
exception is raised) left that exact repository unable to integrate
anything, ever again, with no UI action anywhere able to clear it.
Confirmed real via a direct `_lock()`/`_lock()` reproduction. Fixed:
`IntegrationService.reconcile_on_startup()` (new, wired at app
startup) unconditionally clears the table — every row is inherently
transient, there is no legitimate case for one surviving a restart.
Proven with a real restart, `tests/test_integration_lock_stuck_
after_restart.py`, 2/2 pass.

**P0, fixed — a stale `task_reservations` row permanently blocked one
Task from the parallel-execution-wave scheduler.** `ExecutionWaveService`'s
own atomic single-writer claim (`task_reservations.task_id` as a real
PRIMARY KEY) is released "the moment a launch succeeds or fails" per
its own module docstring — but that reasoning only covers the launch's
two LOGICAL outcomes, not the process dying between the INSERT and the
DELETE a few lines later in the same call. Confirmed real via a direct
INSERT-collision reproduction, and via a full real wave-scheduling run
that silently skips the stale Task forever. Fixed:
`ExecutionWaveService.reconcile_on_startup()` (new, wired at app
startup) unconditionally clears the table. Proven with a real restart
and a real subsequent wave run that successfully launches the
previously-stuck Task, `tests/test_execution_wave_reservation_stuck_
after_restart.py`, 2/2 pass.

**P1, fixed — duplicate-click race on baseline-failure reproduction.**
Found while re-verifying the earlier sweep was complete: `GateWaiverService.
start_reproduction()` had no duplicate-click guard at all (unlike
every other background-work route in this app) — two real concurrent
clicks raced onto the same reused probe worktree path, one failing
with a confusing raw git error. Fixed with the same duplicate-click-
guard convention already used everywhere else (reflect the existing
in-flight run back). Proven with a real double-click (realistic
timing, not a pathological same-microsecond thread race — matching
this codebase's own accepted race-tolerance level elsewhere, e.g.
`OperationService.begin()`'s identical SELECT-then-INSERT shape),
`tests/test_baseline_reproduction_duplicate_click.py`, 1/1 pass.

Audit method for this pass: enumerated every `PRIMARY KEY`/`UNIQUE`
constraint in the schema used as a natural-key lock (2 found, both
fixed), every `status IN (...)`-shaped busy-gate again (no new
instance beyond the five from the first pass), every `threading.Thread`
background-work site again (`gate_waiver_service.py` — the one file
not yet individually checked in the first pass — found the duplicate-
click gap above; its own use of the shared `test_runs` table is
already covered by `TestRunner.reconcile_on_startup()`'s existing
type-agnostic query), and every human-workflow status transition that
merely *looks* busy-shaped (`incidents.REPRODUCING`, `product_
acceptance.PENDING`) — confirmed by reading the actual code that these
are correctly gated on a human's own next action, not a background
worker's, so a restart loses nothing they were waiting on anyway.

## Stop condition

PASS requires every P0 area closed (fixed or evidenced as already
correct) and P1 areas addressed to the bounded extent time/evidence
allows — this document's own acceptance criteria are the complete
scope. Do not expand into new product features. Do not start a new
track automatically after this one closes.
