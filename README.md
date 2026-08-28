# ProjectFlow Workspace Manager

## What it does

A local-only admin web app enforcing:

```text
ONE AGENT → ONE BRANCH → ONE WORKTREE
INTEGRATE SEPARATELY
TEST BEFORE MAIN
```

It creates real Git worktrees, combines selected agent branches in a dedicated integration worktree, executes the managed repository's declared `PROJECT.yaml` CI gates in the background, and only records `READY_FOR_MAIN` for the exact tested commit. It never pushes, merges `main`, force-resolves conflicts, or bypasses GitHub PR/CI.

For Codex and Claude workspaces, **Open Codex** / **Open Claude** launches the
owner CLI in a desktop terminal already positioned at the validated worktree.
The launcher is local-desktop only, accepts no browser-supplied command or
path, and reports missing CLI/terminal/session prerequisites explicitly.

## Quick Start

```bash
cd /home/dell/workspace/projectflow-workspace-manager
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'

./scripts/preflight.sh
./scripts/test.sh

uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. Runtime configuration is documented in `.env.example`; the SQLite DB defaults to `data/workspace-manager.db` and is ignored by Git.

## Stable local service

The supported persistent setup is a per-user systemd service. It always binds
`127.0.0.1:8765`, starts at boot through the user's `default.target`, and uses
`Restart=on-failure`. User lingering must remain enabled for start-at-boot
before interactive login (`loginctl show-user "$USER" -p Linger`).

Create `.env` only when overriding non-secret runtime settings; it is optional
and is read by both systemd and `scripts/start.sh`:

```bash
cp .env.example .env
./scripts/install-systemd.sh
```

The installer is idempotent. If a healthy manually-started Workspace Manager
already owns port 8765, it enables the service but deliberately leaves that
process untouched. Stop the known foreground process before starting the unit.
If another application owns the port, startup fails with `PORT_CONFLICT`.

Operations:

```bash
# Foreground start (development)
./scripts/start.sh

# Persistent service
systemctl --user start workspace-manager.service
./scripts/stop.sh
./scripts/restart.sh
./scripts/status.sh
./scripts/logs.sh          # last 100 journal lines
./scripts/logs.sh 250      # chosen bounded line count
```

The service unit is generated from `systemd/workspace-manager.service.in` with
the current absolute project path as `WorkingDirectory`. Re-run the installer
after moving the project. No script kills a port owner or binds `0.0.0.0`.

## Golden Workflow

```text
Create Agent Workspace
→ Agent Codes
→ Mark Ready
→ Create Integration
→ Merge Branches
→ Run Tests
→ READY_FOR_MAIN
→ push integration branch
→ GitHub Pull Request
→ required CI
→ main
```

Register repositories explicitly on `/repositories`. Discovery is advisory and limited to Git roots at the allowed root or one level below it. Managed repositories do not need to live inside this application repository. All resolved repository paths must stay under `WORKSPACE_MANAGER_ROOT`; all worktrees must stay under `WORKSPACE_MANAGER_WORKTREE_ROOT` (default: `<allowed-root>/.worktrees`). Worktree names include the repository slug (`<repo>-<agent>-<task>`) to prevent cross-repository collisions. Names are normalized to safe slugs and the UI never accepts raw Git commands.

Tests are persisted as short output tails (50 KB per stream) and run in a daemon background thread. This is appropriate for the single-process local MVP; queued/running jobs are not resumed after a server crash. A production/multi-worker deployment, authentication, GitHub automation, embedded terminals, and automatic main merges are explicitly outside V1.

## Safety Model

- One agent owns one branch and one dedicated worktree.
- Agent branches integrate in an `integration/*` worktree, never directly in `main`.
- Repository and worktree paths are resolved and constrained to the configured root.
- A source update invalidates old readiness; tests must PASS at the exact current HEAD.
- `READY_FOR_MAIN` means ready to open a PR, not merged to main.
- Workspace Manager never pushes, bypasses GitHub CI, force-resolves conflicts, or merges main.

## Running locally

For foreground development use `./scripts/start.sh`. For reboot-safe operation
use `./scripts/install-systemd.sh`; see **Stable local service** above for start,
stop, restart, status, and journal commands. Both modes enforce
`127.0.0.1:8765`.

## Testing

```bash
./scripts/preflight.sh
./scripts/test.sh
./scripts/smoke.sh
```

The suite uses disposable temporary Git repositories for real worktree, merge,
conflict, stale-readiness, cleanup, and web-flow coverage. It does not mutate a
registered repository.

## Web Guide

Open <http://127.0.0.1:8765/help> for the Vietnamese operator guide, status
glossary, contextual examples, and a visual-only demo. The demo renders sample
Codex/Claude/integration data but creates no repository, branch, or worktree.
# projectflow-workspace-manager
