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

## Verification UX (V1)

- When you consider your source change complete, report back using the
  format in `templates/agent-completion-report.md` (WORK_STATUS /
  WHAT_CHANGED / AUTOMATED_TESTS / HOW_TO_VERIFY / EXPECTED_RESULT /
  TEST_DATA / RUNTIME_REQUIREMENTS / RISKS). Paste it into the workspace's
  **+ Agent Report** form so it renders under Verification on the
  workspace/Task page instead of only living in chat history.
- `Mark Ready` / `agent_workspaces.status='READY'` means only "source
  change complete, ready for verification/integration" -- never "sandbox
  tested", "manually verified", "integration passed", or "ready for
  main". Do not imply otherwise in any report or commit message.
- Never invent `HOW_TO_VERIFY` steps for a change you did not make. An
  absent report must render as an honest empty state, not a guessed one.
- A recorded manual verification PASS is tied to one exact
  `(sandbox_id, source_commit)` pair (`manual_verifications`) and must
  never be treated as still valid once that source commit is no longer
  the branch's current HEAD -- the UI recomputes staleness from git at
  render time; nothing here stores a second "is it still valid" flag.

## Task-first control plane (V1)

- A Task's DB `status` only ever holds BACKLOG / PREPARE / MERGED /
  CLOSED / CANCELLED -- five explicit, user-action-driven states with no
  natural derivation. DEVELOPMENT / REVIEW / QA / INTEGRATION /
  READY_FOR_MAIN are computed live by `task_stage()` from real workspace/
  review/QA/integration state on every read. Never add a route that
  writes one of the derived values directly into `tasks.status`.
- `Select for Development` (BACKLOG -> PREPARE) allocates nothing.
  `add_task_workspace()` refuses to run on a BACKLOG task -- a branch/
  worktree/sandbox may only be created after Select.
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
