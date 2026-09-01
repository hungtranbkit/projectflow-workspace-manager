# B7 — Repository Identity Durability + Final Track-B Qualification

**Status: IMPLEMENTED (the locally-solvable subset — see Non-goals).**
The phase after B6 (Trusted Reverse-Proxy Support, PASS). No `B7` spec
existed in the repo before this document.

## Grounding: what "workspace identity is permanent" actually means today

Re-read `docs/ARCHITECTURE.md`'s own documented finding before assuming
the problem statement — it is narrower and more specific than the
general "identity model" framing:

- `agent_workspaces`/`integration_workspaces` already have real,
  durable identity: `id INTEGER PRIMARY KEY`. Every FK in this schema
  (tasks, reviews, merge_records, deployments, releases, ...)
  references that integer, never a path or name. **This part is
  already correct and was never broken.**
- The documented limitation is narrower: `branch`/`worktree_path` are
  DB-level `UNIQUE`, rows are never deleted (`remove_task_worktree()`
  only ever sets `status='CLOSED'`), and the name is 100%
  deterministic from `(repository, agent, task-slug)` — so the exact
  same name can never be reused for that same triple again. This is a
  **deliberate, proven, working design** (E13's own suite, the golden
  fixture) with an established, documented, working recovery pattern
  (a new Task, the old one marked `CANCELLED`) — not a bug. Re-
  examined here, per this phase's own instruction to prove
  insufficiency before touching it: **still no evidence of
  insufficiency found. Not touched in B7** — changing UNIQUE/permanent-
  history semantics on a proven, load-bearing design would be a real
  regression risk with no offsetting benefit shown.

**The real, previously-unaddressed gap is one level up**:
`repositories.repo_path TEXT NOT NULL UNIQUE` — a repository's
identity IS its filesystem path, with no other durable fingerprint at
all. Concretely, grounded in the actual registration code (`app/main.
py`'s `register()`, `POST /api/repositories`):

- **A renamed or moved repo directory creates an orphaned duplicate.**
  Re-registering the same logical repo at its new path doesn't match
  the `UNIQUE(repo_path)` constraint's old value, so it inserts a
  BRAND NEW row — disconnected from every Task/Change/Release/evidence
  row the OLD row's `id` still owns.
- **No duplicate-repository detection at all** — two different paths
  pointing at clones of the same logical repository register as two
  completely unrelated rows, silently, with no signal either way.
- **`repositories.github_owner_repo` (B4.1) is cached forever, never
  invalidated** — if an operator's remote changes (a real, named
  scenario in this phase's own instructions), webhook routing silently
  keeps using the stale value. A real, if narrow, staleness bug found
  during this phase's own audit.

This is the real, bounded, locally-provable identity gap — **B7.1's
actual scope.**

## Design: a real git-derived fingerprint, not path or remote alone

Per this phase's own instruction ("do not assume remote URL alone is
sufficient... account for remote changes... handle local-only
repositories"): the fingerprint is the repository's **root commit
SHA(s)** (`git rev-list --max-parents=0 HEAD`, sorted, SHA-256-hashed
together into one comparable value) — DERIVED_TRUTH, computed by a
real local git call, matching `docs/SOURCE_OF_TRUTH.md`'s own
established vocabulary. Chosen specifically because it:

- Survives a path rename/move, a machine move, a fresh clone, and a
  remote URL change — none of those touch commit history.
- Works for a **local-only repository with no remote at all** — remote
  URL alone would be `None` there; root-commit SHA is unaffected.
- Never touches the remote URL itself for identity, so no credential-
  embedded-in-remote leak risk (this phase's own explicit warning) —
  `git rev-list` never even reads the remote config.

**Known, accepted limitation, stated plainly, not hidden:** a shallow
clone (`git clone --depth=N`) has a synthetic "grafted" root at its
shallow boundary, not the true history root — two shallow clones of
the same repo at different depths (or a shallow vs. full clone) can
fingerprint differently. ProjectFlow's own registration flow always
operates on operator-supplied local checkouts (never a CI-style
shallow clone by design), so this is a real but low-probability edge
case for this codebase's actual usage pattern, not fixed in this pass.

## Scope

**B7.1 — Repository identity durability.**
- Migration 37 (additive): `repositories.git_fingerprint TEXT`
  (nullable — an empty/commit-less repo has none, never a guess).
- `GitWorkspaceService.repo_fingerprint(path)`: the real git call
  above, `None` on any failure (no commits, not a git repo, etc.).
- `register()` (`POST /api/repositories`) policy, evidence-based, never
  guessing: (a) re-registering the SAME path — today's exact existing
  update-in-place behavior, unchanged, fingerprint (re)computed. (b) a
  NEW path whose fingerprint matches EXACTLY ONE existing row, AND that
  row's own OLD path is confirmed missing from disk right now —
  **deterministic evidence of a move**: auto-rebind (update the
  EXISTING row's `repo_path`, preserving its `id`/org ownership/every
  FK'd record; `github_owner_repo` cleared so it recomputes fresh,
  closing the remote-staleness gap above). (c) a NEW path whose
  fingerprint matches an existing row whose OLD path still exists on
  disk — **ambiguous** (could be a real duplicate registration, or a
  deliberately-kept-separate second clone, this phase's own explicitly
  named case) — registers as its own new row (today's exact existing
  behavior, never silently merged) but flagged `possible_duplicate_of`
  in the response and surfaced on `/repositories`, a human decides,
  never auto-merged. (d) no fingerprint match — ordinary new
  registration, unchanged.
- Startup backfill (`create_app()`, bounded, idempotent): computes
  `git_fingerprint` for any existing row where it's still `NULL` and
  the path currently exists — closes the "migrating existing
  production-like records" requirement without a one-off script, and
  makes every restart self-healing for rows registered before B7.
- `/repositories` page: shows a "path not found" indicator (a live,
  computed check — `Path(repo_path).is_dir()`, never a stored/stale
  flag) for a row whose directory is currently missing, and a "possible
  duplicate of #N" indicator wherever `git_fingerprint` collides with
  another row.

**B7.2 — Residual local debt**, re-verified with real evidence, not
assumed:
- `review_runs`/`latest_review()` filter — **verified as already
  correctly bounded**, not touched: the two real production gates
  (`review_fix_orchestrator`, `WorkflowService._gate_*`) already filter
  by `review_kind` correctly; `latest_review()`'s own imprecision is
  read by nothing that makes a real decision on it (grep-confirmed,
  see Findings). Marked non-issue, not cosmetic churn.
- `SECURITY_PASS` gate docstring — **real, trivial, fixed**: the stale
  sentence predating `SecurityReviewService` is corrected.
- Dashboard/UI complexity — re-confirmed no new instance beyond what
  B5 already closed; the 13-tab Simple/Advanced item remains a real,
  but non-security, non-bounded UX proposal (P0.12), correctly left
  for a future, dedicated UX pass, not B7.

**Webhook → merge/gate decisions**: re-evaluated once, per this
phase's own instruction. No real GitHub App has been registered and no
real webhook delivery has been observed in this environment during
B3-B7 — the exact evidence this decision requires (ordering, retry,
duplicate-delivery, staleness behavior under a REAL delivery stream)
still does not exist locally and cannot be fabricated. **Stays
BLOCKED, unchanged.** The current safe, live-polled `pr_status()`
decision path is untouched.

## Non-goals (explicit)

- Any change to `agent_workspaces`/`integration_workspaces`' own
  permanent-history/UNIQUE design (re-examined, not proven
  insufficient, see Grounding above).
- Wiring webhook data into any merge/gate decision (BLOCKED, unchanged).
- Shallow-clone fingerprint normalization (known, accepted limitation).
- UI Simple/Advanced mode split (P0.12, out of B7's bounded scope).

## Acceptance criteria

1. A renamed/moved repo directory, re-registered at its new path,
   rebinds the EXISTING row (same `id`, same org link, same Task/
   Change/Release history still attached) rather than creating an
   orphaned duplicate — proven with a real git repo, real filesystem
   move, real re-registration call.
2. Two genuinely different, unrelated repositories never fingerprint-
   collide (real evidence: two independently-created repos, distinct
   fingerprints).
3. A deliberate second clone of the same repo at a path that STILL
   EXISTS is never silently merged into the first — registers as its
   own row, flagged as a possible duplicate, both remain independently
   usable.
4. The startup backfill is idempotent under repeated restarts — run it
   three times against the same DB, identical end state each time, no
   duplicate work, no error on an already-fingerprinted row.
5. `github_owner_repo` is correctly cleared (forces recomputation) on
   a rebind, closing the remote-change staleness gap.
6. Tenant/org ownership and every dependent record (Task, Change,
   Release, merge_records, ...) remain attached to the correct
   (unchanged) `repositories.id` through a rebind — proven directly,
   not assumed from "the id didn't change."
7. `AUTH_MODE=none` behavior unaffected; full regression stays green.

## Acceptance criteria — evidence

All 7 proven by `tests/test_b7_repository_identity.py` (11 tests, real
git repos, real filesystem renames/clones, real re-registration calls,
real restarts via repeated `create_app()` against the same DB file; no
mocked git):
`test_renamed_repo_directory_rebinds_not_duplicates` (1, 6),
`test_two_unrelated_repos_never_fingerprint_collide` (2),
`test_second_live_clone_registers_separately_and_is_flagged_not_merged`
(3), `test_repo_identity_survives_projectflow_restart` +
`test_startup_backfill_fingerprints_pre_b7_rows` (4),
`test_rebind_clears_stale_github_owner_repo` (5),
`test_rebind_preserves_org_ownership` +
`test_duplicate_flag_respects_tenant_isolation` (6, plus tenant-
isolation-of-the-duplicate-signal itself, not explicitly asked for but
verified since `_filter_rows` runs before `_repositories_with_identity_
flags`). (7) verified by the full fast regression run below (default
`Settings()` is `AUTH_MODE=none`; every non-`two_org_fixture` test in
the new file also runs under that default). Two further adversarial
cases beyond the 7 numbered criteria were also proven:
`test_duplicate_enrollment_same_path_updates_in_place` (unchanged
pre-B7 behavior for the common case) and
`test_ambiguous_multiple_missing_candidates_never_guesses` (two
missing candidates sharing a fingerprint — genuinely ambiguous — falls
through to a new row rather than picking one).

## B7.3 — fresh code-grounded audit

Every named subsystem re-checked directly against current HEAD (not
against a previous report). B7's own diff (`git diff --stat`: `app/
db.py`, `app/main.py`, `app/services/git_workspace.py`, `app/static/
style.css`, `app/templates/repositories.html`, `docs/TECHNICAL_DEBT.md`
— 6 files) is small and contained; only the repository-registration
surface changed code, so this audit both verifies that surface directly
and confirms every other subsystem is untouched by this diff and still
wired exactly as B0-B6 last verified it.

- **AuthN / AuthZ read+write isolation**: `register()` kept its
  pre-existing `_require_login_only(request)` guard verbatim (git diff
  shows it unmoved, just now preceded by the new rebind logic); `/repositories`
  GET kept its pre-existing `_filter_rows(..., _visible_repo_ids(request))`
  tenant filter, applied BEFORE the new `_repositories_with_identity_flags()`
  — duplicate-flag computation only ever sees rows the caller could
  already see, so it cannot leak a same-fingerprint match across a
  tenant boundary (proven directly by `test_duplicate_flag_respects_
  tenant_isolation`, not just argued). No new route was added; no
  existing guard was removed, loosened, or reordered relative to CSRF.
  ALREADY_RESOLVED (no regression).
- **CSRF**: `register()`'s `Depends(_mutating_csrf)` is unchanged
  (same dependency, same position in the signature). ALREADY_RESOLVED.
- **Rate limiting**: `slowapi.Limiter` wiring (`app/main.py:433-435`)
  untouched by this diff. ALREADY_RESOLVED.
- **Proxy trust**: `ProxyHeadersMiddleware` wiring (`app/main.py:481`)
  untouched. ALREADY_RESOLVED.
- **Sandbox execution**: `SandboxedCommandRunner`/`SandboxManager`
  wiring untouched; B7's own new code (`repo_fingerprint()`) runs a
  single local, read-only `git rev-list` call through the existing
  `GitWorkspaceService._run()` (same subprocess wrapper every other git
  operation already uses, same audit trail, same `shell=False`) — no
  new execution surface. ALREADY_RESOLVED.
- **Secrets / redaction**: `repo_fingerprint()` never reads remote
  config, never touches `SecretsService`, never logs its own SHA-256
  input/output anywhere sensitive — the fingerprint itself is not a
  secret (it's derived from commit SHAs, which are already public
  within the repo). ALREADY_RESOLVED.
- **GitHub App foundation / webhooks**: `hmac.compare_digest`-based
  webhook signature verification (`app/main.py:1330-1331`) untouched.
  B7 added exactly one interaction with GitHub-related state:
  `github_owner_repo=NULL` on rebind, a strict correctness improvement
  (closes B4.1's remote-change staleness gap) that only ever clears a
  cached value, never fabricates one. ALREADY_RESOLVED. Webhook→merge/
  gate decision wiring: re-evaluated per B7's own instruction, still
  BLOCKED — see below.
- **Release concurrency**: `merge_records`/deterministic-version retry
  logic (B2) untouched by this diff.
- **Workspace/repository identity**: repository identity is this
  phase's own primary deliverable (see above). Workspace *slot*
  identity (`agent_workspaces`) re-examined once more, no new evidence
  found that changes B1-B6's own repeated conclusion — remains
  IMPORTANT, not a blocker, unchanged.
- **Migrations**: migration 37 is a single additive `ALTER TABLE
  repositories ADD COLUMN git_fingerprint TEXT` (nullable, no default
  needed) — safe on a populated production-like table, backward-
  compatible (old code that doesn't know the column still works),
  idempotent via the existing `schema_migrations` version-skip
  mechanism. Verified against a simulated pre-B7 row in
  `test_startup_backfill_fingerprints_pre_b7_rows`.
- **Runtime/deployment assumptions**: unchanged; 127.0.0.1 bind-by-
  default posture untouched by this diff.
- **Audit logging**: a rebind now emits a real `db.event("repository",
  old_id, "REPOSITORY_REBOUND", "old_path=... new_path=...")` row —
  new, additive, follows the exact same `db.event()` convention every
  other mutating route already uses.
- **Error/failure handling**: `register()`'s existing `GitSafetyError`
  paths (invalid repo, missing default branch) are unchanged and run
  BEFORE any fingerprint/rebind logic — an invalid path never reaches
  the new code at all.

No BLOCKER or IMPORTANT item was found that B7 introduced or that is
both locally-solvable and still open. Classification of every item
above: ALREADY_RESOLVED (verified unaffected/correct) except workspace
slot identity (IMPORTANT, unchanged, deliberate) and the webhook→
decision item (EXTERNALLY_BLOCKED, see below).

## Webhook → merge/gate decisions — B7 re-evaluation

Re-checked once, as required. No change to the environment since B6:
no real GitHub App installation, no real webhook delivery endpoint
reachable from GitHub, no way to observe real-world ordering/retry/
duplication/staleness behavior of actual deliveries. `merge_records.
webhook_*` columns (B4) continue to exist purely as an ingestion/
audit trail; `github_merge_service`'s live-poll decision path (`gh pr
view`-equivalent, real-time, always-current) remains the sole
authority for any merge/gate decision — untouched by this diff.
Missing evidence, precisely: at least one real webhook delivery
received and independently verified against GitHub's own timeline for
ordering/duplication/staleness before that data could ever be trusted
as authoritative for a decision, per this phase's own explicit
instruction not to promote unverified webhook data to authoritative
status without proof of the consistency model. EXTERNALLY_BLOCKED
(infrastructure: a real GitHub App installation with a publicly
reachable webhook endpoint), not a local blocker — the system remains
fully safe and correct without it (current live-lookup decision path).

## Regression evidence

New B7.1 test file: `tests/test_b7_repository_identity.py`, 11/11 pass.

Fast "non-real" full regression (`--ignore` real-CLI files + `-k not
(...)`, per the project's own established exclusion list): run 4 times
total during B7 (excluding one run killed early by a 590s shell
`timeout` wrapper before completion, no evidence either way from that
one). Run #3: 1152 passed, 10 failed — all 10 in sandbox-provisioning-
timing tests (`test_sandbox_reset_data.py`, `test_task_sandbox_views.py`,
`test_verification_ux.py`, `test_workflow_dead_anchors.py`,
`test_workflow_decision_ux.py`), every failure a `wait_status(...)`
polling loop observing `'FAILED'` instead of `'RUNNING'` — a simulated-
sandbox timing/resource-contention symptom, not an assertion about
anything B7 touched (B7's diff never touches sandbox provisioning code
at all — `git diff --stat` confirms only `app/db.py`, `app/main.py`
(repositories route only), `app/services/git_workspace.py`, templates/
CSS, docs). Isolated re-run of exactly those 5 files alone: 45/45 pass
clean. Full regression re-run #4 immediately after: 1162/1162 pass
clean, zero failures. Reproduced, isolated, and compared against a
same-code re-run per this phase's own regression-triage rule — real,
transient, resource-contention-under-full-suite-load flakiness on this
shared host, not a B7 regression. (This is a similar category of
environment-only flake to the two known-flaky real-CLI tests already
documented in this project's own memory, extended here to cover a
second, independent flaky-under-load symptom — sandbox provisioning
timing rather than real-Claude-CLI non-determinism.)

## Stop condition

This document's own acceptance criteria, B7.2's real findings, and the
final B0-B7 qualification below are the full extent of B7. Do not begin
B8 automatically.
