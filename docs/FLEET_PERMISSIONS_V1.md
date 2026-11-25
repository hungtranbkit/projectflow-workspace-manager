# Fleet permissions v1 — issue 83

This is an opt-in, exact-worktree profile. It does not change existing sessions,
production services, managed policy, or repository-wide trust. Audit is the
default. A successful process exit is not a successful task: independently read
the requested artifact or remote ref before recording task success.

## New node and rollback

Run as the account that owns the worktree, from this checked-out implementation:

```sh
python3 scripts/fleet-permissions.py audit --workspace /absolute/owned/worktree
python3 scripts/fleet-permissions.py apply --workspace /absolute/owned/worktree --trust-owned-workspace
python3 scripts/fleet-permissions.py check --workspace /absolute/owned/worktree
python3 scripts/fleet-permissions.py preflight --workspace /absolute/owned/worktree --agent codex
python3 scripts/fleet-permissions.py launch --workspace /absolute/owned/worktree --agent codex --prompt-file /absolute/task-prompt.txt --timeout 180
python3 scripts/fleet-permissions.py rollback --workspace /absolute/owned/worktree
```

The worktree must be isolated, owned, clean at apply, and on an `agent/<agent>/<task>`
branch. Read and explicitly authorize its instructions/configuration first.
The profile records canonical workspace, branch and agent-configuration hashes.
Later config changes require renewed trust. WIP after apply is preserved.
State defaults to `~/.local/state/projectflow/fleet-v1`; backups and checkpoints
are private files. Repeat apply and rollback are no-ops. Rollback refuses to
overwrite a profile changed since apply. No login/configuration file is copied.

Linux/macOS also have `scripts/fleet-permissions.sh`. Windows has
`scripts/fleet-permissions.ps1 -Action audit -Workspace C:\owned\worktree`;
use a non-elevated owner terminal, then `-Action apply -TrustOwnedWorkspace`.
Windows/macOS runtime verification remains pending an online authorized node.

`install-gh` installs only the pinned official Linux amd64 GitHub CLI into the
current account, verifies the release archive SHA-256, and preserves any existing
`~/.local/bin/gh`. `rollback-gh` checks the owned install receipt before removing
only its link and binary. Add `~/.local/bin` to this task's PATH; no system PATH
or service is modified. Release: [GitHub CLI 2.105.0](https://github.com/cli/cli/releases/tag/v2.105.0).

An absent or invalid login requires **that node's account owner**, in their own
interactive terminal, to run these once (never paste the resulting credential
into a task, report or another node):

```sh
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git --hostname github.com
```

Then rerun audit and the exact-worktree `git-canary` action. It first requires exactly one effective push URL equal to the verified fetch
URL, then creates a random task-owned local branch/worktree, commits a marker, normally pushes that exact
ref, reads back its SHA, deletes only that remote ref using an advertised-SHA pre-push guard, and verifies absence.
Its local branch remains as a durable checkpoint. On failure preserve the
checkpoint; inspect the exact remote ref before cleanup. Never retry a push by
force or reset an existing branch.

## Launch and policy boundaries

`TerminalLauncherService.launch_trusted_agent` exposes the explicit profile to
Python callers. It preflights before opening a desktop terminal; the child
revalidates before dispatch. The CLI works headlessly. Existing UI/default
launchers are not silently migrated; their historical bypass flags are outside
this profile and must not be described as fixed by this pilot.

Codex uses `exec`, `workspace-write`, `approval_policy="never"`, network inside
that sandbox and `check_for_update_on_startup=false`. Claude uses noninteractive
print mode and `dontAsk` with file tools; shell and connector approvals are not
implicitly granted. `DISABLE_AUTOUPDATER=1` suppresses the documented Claude
updater only. Unknown CLI flags fail preflight. No trust/security dialog is
auto-answered. References: [Codex configuration](https://developers.openai.com/codex/config-reference/),
[Claude permissions](https://code.claude.com/docs/en/permissions),
[Claude settings](https://code.claude.com/docs/en/settings).

Shell execution, Git authentication, GitHub connector approvals, Terminal MCP
read/send grants and OS privileges remain distinct. This pilot has not verified
a per-tool GitHub connector grant for all fleet CLI versions. Do not treat file
tool permission or `read_send` as permission to comment, create PRs or mutate an
unrelated repository. Those operations need the explicit owner-authorized task
and a verified delivery lane.

HP native Codex cannot create its required user namespace. An explicit Linux
Docker file-work canary uses `--runtime docker --image sha256:<existing-image-ID>`.
It runs as the current non-root UID, drops capabilities, enables no-new-privileges,
uses a read-only root, and mounts only the exact workspace, installed CLI binaries,
and the same node's original auth file read-only. No Docker socket, host home or
Git credential is mounted. It is **not a Git-capable agent runtime**: linked Git
metadata is not mounted. Commit/push verification runs separately on the host.
Do not use it to claim native sandbox repair or end-to-end autonomous delivery.

All four sudo checks failed. Current user-level repairs need no administrator.
Reports mark `OWNER_BOOTSTRAP_REQUIRED` separately from task-required checks.
There is no approved root service wrapper present in this pilot, so no root
operation is dispatched. A general Docker-group grant is root-equivalent and
is not a scoped repair. If an administrator requires a service wrapper, its
exact allowlisted operation, target account, immutable implementation path and
root-owned install artifact must be reviewed before an install command can be
specified safely. Do not grant global NOPASSWD or change kernel/user-namespace
policy to make this test pass.

## Terminal MCP source audit

Audited actual HP source at commit `28ec55f3596926302e38b645e2efff64f36dbd23`.
Existing dirty `config.yaml`, `PROJECT_CONTEXT.md` and backup were preserved.
`terminal_mcp/controller.py:859` budgets resolution and node I/O together;
`compact_tools.py:469` returns `STATUS_PROBE_TIMEOUT` for exhausted budgets,
an existing in-flight read for that key, 32 concurrent readers, or a Future timeout.
It does not diagnose sudo, Git auth or a rejected CLI flag. Use qualified
`node/session` to avoid fleet-wide bare-name resolution. Preserve the durable
request/resume identity and do not redispatch just because status is unknown.

Session cwd resolves through the configured allowed roots; `grant_mode` supports
`none`, `read`, `read_send`. Binding/session access is separate from the CLI's
own trust and policy. This run used dedicated generic shell sessions only for
Dell/NEC probes, and the existing approved user SSH config for M910. The earlier
failed attempts were not dispatched. Current qualified shell probes completed;
one NEC install wait exhausted its synchronous budget, then the same continuation
confirmed completion without resending. No claim is made that the historical
timeout cause or Terminal MCP server has been fixed.

## Evidence and acceptance

See [the measured node record](evidence/fleet-83/README.md). Issue 83 remains open:
remote Git auth, native HP sandbox policy, connector-tool grants, full application
launcher integration, and offline-platform runtime checks are not all satisfied.
Do not recover historical Git-blocked work based only on this pilot. Recovery
requires exact old branch/commit authorization, a durable checkpoint, current
permissions, and normal reviewed delivery from the HP lane if needed.
