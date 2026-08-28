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
