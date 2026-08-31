# ProjectFlow Workspace Manager agent rules

This file and `PROJECT.yaml` are the standalone repository contract.

- ONE AGENT, ONE BRANCH, ONE WORKTREE.
- INTEGRATION SEPARATELY.
- MAIN ONLY THROUGH PR + CI.
- Develop only in an assigned `agent/<agent>/<task>` branch and worktree.
- Never create an agent worktree inside this project repository.
- Never bypass GitHub PR or CI and never merge, reset, or force-push `main`.
- All application Git execution belongs in `app/services/git_workspace.py`.
- Commands are argv lists with `shell=False`; validate resolved paths and branch names first.
- Preserve unrelated WIP. Never clean or reset a managed repository.
- Read each managed repository's `PROJECT.yaml`; do not hard-code its tests.
- Managed repositories are independent and may live anywhere below the configured allowed root.

## Sandbox & Cross-Repo Integration (V1)

- A Task may span many repositories. Every `AgentWorkspace` inside a Task
  is still exactly ONE repo + ONE branch + ONE worktree -- a Task never
  creates one Git worktree spanning multiple repos.
- Cross-repo dependency testing must use integration source
  branches/worktrees. Do not merge dependency code into `main` merely to
  make another agent's repo testable -- build a Task Integration and an
  Integration Sandbox from the unmerged branches instead.
- Task sandbox source must be pinned to exact commits (`sandbox_sources`),
  never a branch name alone.
- A verified integration becomes stale the moment any participating
  source commit changes; `READY_FOR_MAIN`/`ready_for_main` must be
  invalidated when that happens, the same way a single-repo integration's
  readiness already is.
- All Docker execution for sandboxes belongs in
  `app/services/sandbox_runtime.py`, with the same discipline as git:
  argv lists, `shell=False`. Every sandbox uses a unique
  `wm-<repo>-<owner>-<suffix>` compose project; cleanup must verify that
  exact namespace (`SandboxRuntimeService.verify_owned`) before touching
  any container/network/volume.
- Never target a `PRODUCTION`-marked runtime/environment from a sandbox.
- Sandbox cleanup removes only sandbox-owned runtime resources
  (containers/networks/volumes/env file/port reservations). It never
  deletes Task metadata, audit events, test results, source manifests,
  git branches, or worktrees.

## Task Lifecycle & Gate Model (V2)

- `tasks.status` only ever holds `BACKLOG` / `ACTIVE` / `CANCELLED` --
  three explicit, user-action-driven values with no natural derivation
  (Select for Development / Cancel Task are the only writers besides
  Create). `BLOCKED`, `READY_FOR_MAIN`, and `DONE` are NEVER persisted to
  this column; they are computed live, on every read, by
  `TaskDecisionService.evaluate()` (`app/services/task_decision_service.py`)
  from real Builder Workspace / ReviewRun / QARun / Integration /
  MergeRecord state. `DEVELOPMENT` / `REVIEW` / `QA` / `INTEGRATION` /
  `MERGING` / `PLANNING` / `COMPLETE` are Task **Stage**, not Task
  status -- also computed by the same service, never a separate column.
  Never add a route that writes any of these derived values directly.
- `TaskDecisionService` is the single source for status, stage,
  `next_action`, `blocking_reasons`, `test_readiness`,
  `integration_eligibility`, and `ready_for_main`. No route or template
  may independently recompute any of these -- `app/main.py`'s
  `task_card_view()` and the `/tasks/{id}` route both call
  `decision.evaluate(tid)` once and pass its result through; `GET
  /api/tasks/{id}/decision` exposes the same result directly for
  automation/tests. If you need a new derived fact about a Task, add it
  to `evaluate()`'s result, don't compute a second copy at the call site.
- `Select for Development` (BACKLOG -> ACTIVE) allocates nothing.
  `add_task_workspace()` refuses to run on a BACKLOG task -- a branch/
  worktree/sandbox may only be created after Select, and even then a
  Builder Workspace is created one at a time, explicitly.
- Risk profile (`LOW` / `NORMAL` / `HIGH`, default `NORMAL`) decides
  which gates are required (`RISK_GATES` in `task_decision_service.py`,
  the ONLY copy of this table -- never re-declare it in `app/main.py`,
  see Workflow Summary UX (V3) below): LOW = Review only; NORMAL = Review
  + Runtime Verification (QA) + Integration; HIGH = the same three, with
  a deeper QA bar. A gate the current risk profile does not require must
  render as `NOT_REQUIRED`, never as a silent/implied PASS.
- Review and QA are real history tables (`review_runs`, `qa_runs`), not
  mutable columns on `agent_workspaces`. "Start Review"/"Start QA" always
  INSERTs a new row; "Submit" only UPDATEs the latest PENDING/RUNNING row
  for that workspace/task. A fix-and-resubmit cycle is a NEW row -- never
  overwrite or delete prior review/QA evidence.
- Every Review/QA/Integration PASS is pinned to an exact commit
  (`reviewed_commit`) and Brief version (`brief_version`). Both are
  recomputed live against the branch's current git HEAD and the Task's
  current `brief_version` in `TaskDecisionService.builder_view()` /
  `evaluate()` -- a new commit or a Brief edit flips a stored PASS row to
  a *displayed* `STALE` without ever writing a stale flag to the DB. Do
  not add a route that tries to "invalidate" evidence by mutating it;
  invalidation is always a live recomputation.
- A Reviewer's default mode is a READ_ONLY review session -- `start_review`
  never creates a worktree. A worktree is only created if a reviewer is
  explicitly escalated to repair mode via the normal Create Builder
  Workspace flow (same as returning work to the original Builder).
- QA is Task-level (`/api/tasks/{id}/start-qa` / `submit-qa`), not
  workspace-level -- it may need to exercise cross-repo behavior and
  primarily reads an existing sandbox + source manifest; it does not
  create a worktree on its own.
- Integration eligibility requires every required Builder Workspace to
  be READY with a current, PASS review and no unresolved FIX_REQUIRED --
  gated by `decision.integration_eligibility["eligible"]`
  (`create_task_integration` in `app/main.py`), never a looser "at least
  one workspace is READY" check. A multi-repo Task gets one Repo
  Integration (`integration_workspaces` row) per participating repo plus
  one Integration Sandbox -- never one worktree spanning repos.
- Cross-repo merge tracking is per-repository (`merge_records`:
  `repository_id`, `merge_status` NOT_STARTED/PR_OPEN/MERGED/FAILED/
  CANCELLED). A Task reaches `DONE` only when every *required*
  MergeRecord is MERGED -- one repo merged and another still PR_OPEN
  must leave the Task at READY_FOR_MAIN/MERGING, never DONE. `mark-merged`
  (bulk or per-repo) only ever writes to `merge_records`; it never writes
  `tasks.status` directly.
- `Close Task` requires computed `status == "DONE"` and only stamps
  `closed_at` -- it is an archive/admin annotation, not a precondition
  for sandbox/worktree/branch cleanup, which stays independent (Task
  Closed != Sandbox deleted != Worktree deleted != Branch deleted).
- Migration note: existing Tasks created before this model (old status
  values like `MERGED`/`CLOSED`/`READY_FOR_INTEGRATION`) are remapped by
  DB migration V6, never blindly inferred -- `legacy_status` preserves
  the original value and `needs_reconciliation=1` flags any row whose
  remap was ambiguous, for manual review rather than a fabricated PASS.

## Prompt-first Task creation UX (V1)

- `tasks.implementation_prompt` is the primary way a Task's intent is
  described now -- one free-text field, not a structured GOAL/CONTEXT/
  REQUIREMENTS/ACCEPTANCE_CRITERIA/OUT_OF_SCOPE/TEST_PLAN form. The
  legacy `brief_*` columns and their form (`/api/tasks/{id}/brief`) still
  exist and still work, purely for backward compatibility with Tasks
  created before this UX existed -- never remove or backfill them.
  `render_agent_prompt()`/`TaskDecisionService.brief_complete()` both
  branch on whether `implementation_prompt` is set; everything else
  (staleness, gates, next_action) treats "the Task's intent" as whichever
  of the two is actually populated.
- Deliberately reuses `brief_version` as the version counter for the
  prompt too (never a second, parallel `prompt_version` column) -- see
  the V7 migration comment in `app/db.py`. Bump it on any actual content
  change (`save_prompt`, mirroring `save_brief`'s own change-detection);
  everything downstream (`TaskDecisionService.builder_view()` flipping a
  pinned Review/QA row to STALE) already keys off this column and needs
  no separate wiring.
- `render_agent_prompt(t, repo_row=None)` never rewrites the user's own
  `implementation_prompt` text -- it only wraps it with real, recorded
  context (repo name/path, Workflow/risk_profile, this repo's own
  `AGENTS.md` if one exists) plus the fixed completion-requirements
  footer. If you add more auto-injected context here, keep the user's
  own text a verbatim, uninterrupted block -- never interleave generated
  text inside it.
- `regenerate_agent_prompt()` is the one place that recomputes the
  derived `agent_prompt` (from `implementation_prompt` + repo context) and
  persists it as a new `prompts` row stamped with the current
  `brief_version`. Call it after anything that changes what the prompt
  should say (Task create with a repo/agent already known, prompt edit);
  never build `agent_prompt` a second way inline in a route.
- `POST /api/tasks/create` is the single, primary Task-create endpoint
  behind the simplified UI (title + prompt + optional repository/agent/
  sandbox, Advanced = base branch/role/Workflow/additional repositories).
  It subsumes both of the older `/api/tasks` (plain BACKLOG) and
  `/api/tasks/new-with-workspace` (BACKLOG-skip quick-start) flows in one
  form -- those two routes are kept, unmodified, for API back-compat, but
  are no longer linked from the primary "New Task" UI.
  `/api/tasks/create`'s Advanced "additional repositories" array uses
  `ws_repository_id`/`ws_agent`/`ws_role`/`ws_base_branch`/
  `ws_sandbox_profile` (`getlist`) -- the *primary* row's own role/base
  branch fields are deliberately named `primary_role`/
  `primary_base_branch` instead, never `ws_role`/`ws_base_branch`. Reusing
  the array's field name for the primary row was a real bug once a second
  Builder Workspace row existed on the same form (the primary value and
  the first array entry silently collided) -- keep the names distinct.

## Builder execution UX with Task Title fallback (V1)

- `effective_task_prompt(t)` / `prompt_source(t)` (module-level functions
  in `app/services/task_decision_service.py`, the one canonical
  implementation -- imported into `app/main.py`, never duplicated) are
  the Task Title fallback: `implementation_prompt` when non-empty, else
  `title` itself (mandatory at Task creation, so always resolvable).
  `TaskDecisionService.evaluate()` exposes both (`prompt_source`,
  `effective_task_prompt`) in its result so no route/template re-derives
  them. Start Builder, Start All Builders, and Task creation itself are
  never gated on `implementation_prompt` being non-empty -- only on Task
  title being non-empty (enforced once, at Task creation).
- `render_agent_prompt(t, repo_row=None, workspace=None, sandbox_line=None)`
  is the one function that composes the actual Builder prompt: TASK (the
  effective, title-or-prompt intent, verbatim) -> TASK TITLE -> REPOSITORY
  -> BRANCH/WORKTREE (when a specific workspace is given) -> SANDBOX ->
  ROLE + optional BUILDER INSTRUCTIONS (workspace-specific) -> RULES (a
  fixed instruction to read AGENTS.md/PROJECT.yaml, plus an excerpt if
  found) -> COMPLETION. The user's own text is confined to its own TASK
  section; RULES/COMPLETION are always separate, later, Workspace-Manager-
  owned sections a Task's text cannot reach into or override.
- `workspace_agent_prompt(w, t=None, repo_row=None)` is the per-Builder-
  Workspace LIVE prompt (role/instructions/sandbox included) -- always
  freshly computed for display (Task Detail, Workspace Detail), never
  stored/cached, so an instructions or role edit shows up immediately
  with nothing to go stale. `regenerate_agent_prompt(tid, repo_row=None)`
  remains the TASK-level (no specific workspace yet) variant, persisted
  to `tasks.agent_prompt` + a `prompts` row, used before any Builder
  Workspace exists and on every prompt edit.
- `_start_builder_session(w, mode)` is the one place a Builder's
  AgentSession actually starts (used by both `POST /api/workspaces/{id}/
  sessions` and `POST /api/tasks/{id}/start-all-builders`) -- validates
  only the trusted agent-launcher registry, never `implementation_prompt`.
  It best-effort snapshots the exact live per-workspace prompt into
  `prompts` (workspace_id set) at start time, purely for audit -- a
  snapshot failure never blocks starting the agent.
- `agent_workspaces.builder_instructions` (migration V8): optional,
  per-Builder-Workspace extra instructions layered on top of the Task's
  shared effective prompt (e.g. distinguishing a Backend Builder's job
  from a Firmware Builder's on the same Task). Never required, never
  separately versioned -- editing it does not bump `brief_version` and
  has no Review/QA staleness implications of its own.
- `TaskDecisionService.builder_view()` now also returns `agent_status`
  (`NOT_STARTED` when no `agent_sessions` row exists at all for that
  workspace, else the session's own real status) -- Workspace Status
  (`CREATED`/`READY`/`CLOSED`) and Agent Status must never be shown or
  reasoned about as the same signal. `_next_action`'s per-builder branch
  now distinguishes `START_BUILDER` (no live session) / `VIEW_BUILDER`
  (session STARTING/RUNNING/WAITING_FOR_INPUT) / `REVIEW_BUILDER_RESULT`
  (session EXITED/FAILED, builder still hasn't submitted) where it used
  to return one undifferentiated `OPEN_BUILDER` for all three. `NEXT_ACTIONS`
  no longer includes `COMPLETE_BRIEF` or `OPEN_BUILDER` -- Task Title
  fallback means "Selected + no Builder Workspace" goes straight to
  `CREATE_BUILDER_WORKSPACE`, unconditionally.
- Caveat: `agent_status` only reflects the web-PTY `AgentSession` path
  (`/api/workspaces/{id}/sessions`). The separate desktop-launcher path
  (`/api/workspaces/{id}/launch-agent`, the "Start Claude"/"Start Codex"
  button's `launchWorkspace(...,'agent',...)` call) is fire-and-forget --
  it records an event but creates no trackable session row, so a Builder
  actually running via that path can still show `Agent: NOT STARTED`.
  This is a pre-existing limitation of the desktop launcher, not
  something this UX introduced; `Start All Builders` deliberately only
  ever uses the trackable web-PTY path so "don't start an already-running
  Builder" is a real check, not a guess.

## Verification UX (V1)

- When you consider your source change complete, report back using the
  format in `templates/agent-completion-report.md` (WORK_STATUS /
  WHAT_CHANGED / AUTOMATED_TESTS / HOW_TO_VERIFY / EXPECTED_RESULT /
  TEST_DATA / RUNTIME_REQUIREMENTS / RISKS). Paste it into the workspace's
  **+ Agent Report** form so it renders under Verification on the
  workspace/Task page instead of only living in chat history.
- `Submit for Review` / `agent_workspaces.status='READY'` means only
  "source change complete at this exact commit, ready for a Reviewer" --
  never "sandbox tested", "manually verified", "Review PASS", "QA PASS",
  "Integration passed", or "READY_FOR_MAIN". Do not imply otherwise in
  any report or commit message. The legacy one-click `/api/workspaces/
  {id}/ready` endpoint still exists for API compatibility but is no
  longer a UI button -- the only way to reach READY through the UI is the
  Agent Report form (WORK_STATUS=READY), which additionally requires a
  clean git worktree and a real completion report.
- Never invent `HOW_TO_VERIFY` steps for a change you did not make. An
  absent report must render as an honest empty state, not a guessed one.
- A recorded manual verification PASS is tied to one exact
  `(sandbox_id, source_commit)` pair (`manual_verifications`) and must
  never be treated as still valid once that source commit is no longer
  the branch's current HEAD -- the UI recomputes staleness from git at
  render time; nothing here stores a second "is it still valid" flag.

## Task-first control plane (V1)

- `AGENT PROMPT` and the Review prompt are both filled by a deterministic
  Python template (`render_agent_prompt` / `render_review_prompt`), never
  a model call. A human reviews/edits before any agent is launched --
  creating an Agent Workspace and creating an AgentSession (actually
  running the agent) are two separate, explicit actions.
- Command safety for the web terminal (AgentSession/PTY): argv is always
  derived from the same trusted `AGENT_LAUNCHERS` registry the desktop
  launcher uses (`app/services/agent_session_manager.py`). A request can
  supply only `workspace_id` and `mode` -- there is no code path that
  accepts a client-supplied cwd, executable, or flags.
- `VIEW_ONLY` vs `INTERACTIVE` is enforced server-side by re-reading the
  live session's own `mode` on every WebSocket stdin message, never by
  trusting anything the client claims about itself.
- A server restart genuinely loses every in-process PTY.
  `AgentSessionManager.reconcile_on_startup()` marks any row still
  RUNNING/STARTING/WAITING_FOR_INPUT as FAILED at boot -- never leave a
  session appearing to run after the process that owned it is gone.
- tmux backing for AgentSession was evaluated (spec section 21) and
  deliberately NOT adopted in V1: `pty.fork()` already gives real,
  per-session PTYs with working resize/stdin/exit-code semantics, and
  every session here is already reconciled honestly across a restart.
  tmux would mainly buy reconnect-after-browser-refresh and
  multiple-viewers-per-session; revisit only if either becomes an actual
  requirement, and test its own lifecycle/cleanup/security discipline
  (same rigor as `SandboxRuntimeService.verify_owned`) before adopting it.
- Never expose `/agents/live`, `/ws/sessions/{id}`, or any session route
  on an interface reachable without authentication. This app still binds
  `127.0.0.1` only; remote access must go through something that
  authenticates first (Tailscale, Cloudflare Access, an SSH tunnel) --
  never a public unauthenticated port.

## Workflow Summary UX (V3)

- Every workflow page (Task Detail, Workspace Detail) leads with a
  Workflow Summary: an evidence-based checklist, exactly one current
  step, missing requirements as plain language, and at most one primary
  action -- never a technical-status dump the user has to interpret
  themselves. Raw implementation state (Workspace Status/Agent
  Status/Sandbox Status/blocker codes/etc.) stays real and available, but
  only under "Xem tất cả chi tiết kỹ thuật" (Advanced), never the default
  screen. `TaskDecisionService.evaluate()`'s result carries this UI's
  three extra ingredients on top of the existing status/stage/
  next_action/blocking_reasons: `checklist` (`_checklist()`),
  `missing_requirements` (`_missing_requirements()`), and
  `previous_step_summary` (`_previous_step_summary()`). `user_task_state()`
  (`app/services/user_state_view.py`) is still the one place that turns
  all of `evaluate()`'s fields into what a template renders (headline/
  explanation/blocker/primary_action, now plus `checklist`/`missing`/
  `previous_step_summary` verbatim) -- templates render this object, they
  never recompute workflow meaning themselves.
- RISK_GATES policy change: Runtime Verification (the `QA`/`qa_runs`
  gate internally -- user-facing label changed, the DB/route/action names
  did not, see below) is now required for `NORMAL` risk too, not only
  `HIGH` -- LOW is the only profile that still skips straight from Review
  PASS to Integration/Ready for Main. `RISK_GATES` in
  `task_decision_service.py` is the only copy of this table now;
  `app/main.py` previously hand-declared a second, already-drifted copy
  (it said NORMAL excluded QA) -- never re-add one, always read through
  `decision.requires_qa()`/`decision.requires_integration()`.
- Runtime Verification is Sandbox-gated (sections 12-15 of the redesign
  spec): starting a fresh QA run when the risk profile requires one and
  the relevant Builder Workspace's own sandbox is actually required
  (`TaskDecisionService.builder_sandbox_state()` -- a repo that declares
  a `sandbox:` contract, unless the workspace explicitly opted out with
  `sandbox_profile=NONE`, mirroring `workspace_readiness()`'s existing
  rule in `app/main.py` exactly) now surfaces `CREATE_SANDBOX` /
  `SANDBOX_PROVISIONING` / `REBUILD_SANDBOX` as real next_action steps
  before `START_QA`, reusing the Builder's own per-workspace sandbox
  (`/api/tasks/{tid}/workspaces/{wid}/create-sandbox`, already existed)
  rather than a second Task-level sandbox concept. An in-flight or
  already-completed QA run (`PENDING`/`RUNNING`/`FAIL`/`BLOCKED`) is never
  re-gated on the sandbox a second time -- only *starting a fresh run*
  checks it (`needs_fresh_run` in `_next_action`).
- Human blocker translation lives in one table,
  `BLOCKER_MESSAGES`/`humanize_blocker()` in `task_decision_service.py`
  (registered as the `humanize_blocker` Jinja filter in `app/main.py`) --
  never show a raw code (`CI_PENDING`, `SOURCE_STALE`, ...) as the primary
  explanation in the normal workflow view; the raw code stays available
  in Advanced/`title=""` only. Extend this one table for any new blocker
  code, never inline a second translation at a call site.
- Section 18 (Task Detail and Workspace Detail must never disagree on the
  one primary action): once a Workspace belongs to a Task, Workspace
  Detail's Current Action panel renders the SAME
  `user_task_state(decision.evaluate())` Task Detail renders (`main.py`'s
  `workspace_detail()` route computes `task_user_state` once, alongside
  the pre-existing task-less `next_action` ladder that still exists only
  for workspaces with no Task at all). Do not let Workspace Detail
  recompute its own opinion about the Task's next real action ever again.
- Checklist completion is evidence-based only (never "the user navigated
  past it"): `AUTOMATED_TESTS` in the checklist is PASS only when a real
  `test_runs` row exists at the Builder's *exact current HEAD*
  (`builder_tests_status()`) -- an agent's own WORK_STATUS=READY claim is
  not evidence of tests passing.

## Spec-Driven Development (Spec Layer, V1)

`specs/` (manifest `specs/SPEC.yaml`) is the canonical, approved
specification for this project's own externally observable behavior --
loaded by `SpecRegistry` (`app/services/spec_registry.py`), gated by
`SpecGate` (`app/services/spec_gate.py`), and verified against real
evidence by `SpecComplianceVerifier` (`app/services/spec_compliance.py`)
and `EvidenceStore` (`app/services/evidence_store.py`). This governs a
different concern from ONE-AGENT-ONE-BRANCH-ONE-WORKTREE / MAIN ONLY
THROUGH PR above (that's Builder Workspace orchestration for *managed*
repositories); this section is about how any agent -- human-directed or
autonomous -- changes THIS repository's own behavior.

Before implementing a change that is not a pure refactor/typo/comment
fix:

- Identify which FeatureSpec, Requirement(s), and Acceptance
  Criterion/Criteria the change belongs to (`GET /api/spec/features`,
  `GET /api/spec/features/{id}`). If none exists yet for genuinely new
  behavior, that is a real blocker -- say so; do not proceed by
  inventing scope.
- Classify the change (`spec_change_classification`:
  `NO_BEHAVIOR_CHANGE`, `BUG_FIX_TO_EXISTING_SPEC`, `BEHAVIOR_CHANGE`,
  `NEW_FEATURE`, `SPEC_CHANGE`, or `AMBIGUOUS`) and, for anything but
  `NO_BEHAVIOR_CHANGE`, link the Task to its feature/requirement/
  acceptance ids via `POST /api/tasks/{tid}/spec`. A Task with no
  classification at all is legacy/unclassified and stays completely
  ungated (`SpecGate` -> `NOT_APPLICABLE`) -- this is intentional
  backward compatibility (REQ-005), not a loophole to leave every new
  behavior-changing Task unclassified.
- `SpecGate.evaluate()` runs automatically in `_start_builder_session`
  (the one real place an Agent session starts) and must return `PASS`
  or `NOT_APPLICABLE` before an Agent is started for a Task; a gated
  classification without a valid, approved, fully-traced feature link
  is refused with a concrete outcome
  (`SPEC_REQUIRED`/`SPEC_NOT_APPROVED`/`TRACEABILITY_MISSING`/
  `SPEC_REFERENCE_INVALID`), never silently allowed through.
- Preserve every listed Invariant. Stay inside the linked feature's
  declared scope (`includes`/`excludes`). Implement the smallest change
  that satisfies the linked Requirements/Acceptance Criteria -- do not
  expand scope, weaken an acceptance criterion, or invent unspecified
  behavior to make something "work."
- Run real verification (tests, review, and Runtime Verification when
  the Task's risk profile requires it -- `RISK_GATES` above) and let it
  generate real evidence in the existing tables
  (`verification_reports`/`review_runs`/`qa_runs`/`test_runs`/
  `manual_verifications`); do not declare a Task done with missing or
  fabricated verification. `EvidenceStore` and
  `GET /api/tasks/{tid}/evidence` are the read path over that evidence,
  never a second copy of it.
- Before calling spec-linked work complete, run
  `GET /api/tasks/{tid}/spec-compliance` (`SpecComplianceVerifier`).
  `PASS` means required evidence is present and passing; `INCOMPLETE`
  means real evidence is still missing; `FAIL` means evidence reports a
  real failure; `SPEC_DRIFT` means the implementation and the approved
  spec (or the spec reference itself) disagree -- in every case but
  `PASS`, keep working or report the disagreement; never reclassify or
  re-scope the spec yourself to force a `PASS`.
- If the approved spec and the real, correct implementation genuinely
  disagree, report `SPEC_DRIFT` explicitly (in the Task/PR/commit
  description) rather than silently picking one side.
- The spec file tree (`specs/**/*.yaml`) is the single source of truth
  (S1); `tasks.spec_*` / `verification_reports.spec_*` columns are only
  ID/version trace pointers into it, never a second authoritative copy
  -- edit specs by editing the YAML under `specs/`, not by reasoning
  from a Task's stored linkage alone.

## Architecture Visualization (Archify)

- [Archify](https://github.com/tt-a1i/archify) (`tt-a1i/archify`) is
  installed project-local at `.agents/skills/archify/` (canonical copy,
  pinned at upstream v2.16.0 / commit `2bfb47132c057195d8dddb3e25ae966dd7c7a72e`,
  `test/` intentionally excluded -- see its own `skill-release.json`).
  `.claude/skills/archify` is a symlink to that same copy -- there is
  only ONE installed copy; do not `cp` a second one for a different agent
  surface. It has zero runtime npm dependencies (`doctor`/`validate`/
  `deliver`/`guide` need no `npm install`); do not add it to
  `pyproject.toml` or touch application runtime dependencies for it.
- Use it when: reviewing or explaining ProjectFlow's own architecture,
  tracing a request/data flow before a cross-cutting change, or
  documenting a new security/tenant boundary (e.g. a future B0.3+ sub-
  phase) -- not for routine single-file work.
- Standard mapping prompt for regenerating the current diagram: read
  `.agents/skills/archify/SKILL.md`, then author/update
  `docs/architecture/projectflow-runtime.architecture.json` (schema
  `architecture`) grounded in the actual code at the repo's current
  HEAD commit, and validate + render with:
  `node .agents/skills/archify/bin/archify.mjs validate architecture docs/architecture/projectflow-runtime.architecture.json --quality showcase --repo-root . --json`
  then
  `node .agents/skills/archify/bin/archify.mjs deliver architecture docs/architecture/projectflow-runtime.architecture.json docs/architecture/projectflow-runtime.architecture.html --quality showcase --repo-root . --json`.
  Run `node .agents/skills/archify/bin/archify.mjs doctor` first if
  either command errors unexpectedly.
- Grounding rule: every component's `sources[]` must cite a real
  `path`/`line` that exists at `meta.repository.revision` (set to the
  actual commit SHA being diagrammed) -- `--repo-root .` makes Archify
  verify this against real Git blame, not trust the author. Never add a
  component just because it appears on the B0 roadmap; only diagram what
  the source at that revision actually shows. Tag anything not fully
  built as `PARTIAL` or omit the `IMPLEMENTED` tag, and say in a
  `sublabel`/card which future sub-phase (e.g. "B0.3", "B0.6") will
  complete it -- never present a planned boundary as already enforced.
- The current diagram (`docs/architecture/projectflow-runtime.architecture.json`
  + rendered `.html`) reflects B0.1/B0.2 (AuthN, Organizations/Tenants)
  as implemented and B0.3-B0.7 as explicitly planned/not-yet-built; it is
  now stale evidence the moment a later B0 sub-phase, or any other
  structural change, ships -- regenerate it then, don't leave it
  describing a superseded HEAD.
