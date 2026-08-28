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
  which gates are required (`RISK_GATES` in `task_decision_service.py`):
  LOW = Review only; NORMAL = Review + Integration; HIGH = Review + QA +
  Integration. A gate the current risk profile does not require must
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
