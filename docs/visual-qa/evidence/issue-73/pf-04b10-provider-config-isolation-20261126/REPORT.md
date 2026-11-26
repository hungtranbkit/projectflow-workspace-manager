# PF-04b10 — Claude test config isolation

Date: 2026-11-26 (ICT, UTC timestamps in captured commands/logs)

## Scope and source identity

- ProjectFlow only; test harness changes only. No application/provider launcher,
  database schema, production, NovaRetail, CI setting, or global auth change.
- Task worktree: `agent/codex/issue-73-provider-test-isolation-20261126`.
- Starting local commit/tree: `b3c0a23a46ee0273dc4954b906700f76bb04654c` /
  `205452217c23ba8ff88dfb2c2022c19a2ea9a58a`.
- PR #74 remote at preflight: `e3755269baa42fb266044a9441f4f8e8979b168e`;
  `git ls-remote` confirmed that exact branch head. The local source parent
  was tree-equivalent, not the same commit. This report is not a claim that
  the changes have been published or tested on the remote SHA.

## Finding and change

The prior real-provider test helper wrote to `~/.claude.json`; its restore
function removed every key under `/tmp/pytest-of-dell/`, which could erase
trust entries belonging to concurrent pytest runs. Some tests registered
trust before entering `try/finally`, so launch/setup exceptions could leave
shared config modified. Real provider tests also lacked a fail-closed check
that their selected isolated config was authenticated.

Added `tests/claude_test_config.py` to create config only below the current
pytest-owned temporary root, reject home/out-of-root/symlink paths, set
`CLAUDE_CONFIG_DIR`, update/restore only the exact test project key, and
register cleanup immediately. It never reads/copies credential values. A
real test can proceed only when `claude auth status --json` reports a logged
in identity for that isolated config. Updated the existing real-provider
test callers to use the helper; provider tests themselves were not invoked.
Added synthetic regression tests including a real `AgentSessionManager` PTY
child environment-propagation check.

## Verification

RED before replacing the old cleanup helper:

```text
PATH=/usr/bin:/bin <existing-venv>/bin/python -m pytest -q \
  tests/test_claude_test_config_isolation.py::test_cleanup_preserves_sibling_pytest_trust_entries \
  --junitxml=red.xml
exit 1; 1 failed
```

The failure showed the sibling `/tmp/pytest-of-dell/pytest-sibling/worktree`
entry had been removed. `red.xml` is whitespace-normalized XML; the exact
pytest XML bytes are preserved as `red.raw.xml.base64` (decode with
`base64 -d red.raw.xml.base64 > red.raw.xml`).

GREEN after the isolated helper and auth check:

```text
PATH=/usr/bin:/bin "$VENV/bin/python" -m pytest --confcutdir=/tmp \
  -q -ra tests/test_claude_test_config_isolation.py --junitxml=green.xml
exit 0; 6 passed
```

The existing task-local environment reported Python 3.14.4 and pytest 9.2.0.
The recorded final run was 2026-11-26 05:03:27–05:03:30 UTC (12:03:27–12:03:30
ICT); its exact stdout, JUnit and exit code are retained here.
`--confcutdir=/tmp` is essential for this self-contained regression module:
it prevents repository `tests/conftest.py` from loading its broad Docker
cleanup fixture. `collect-only` for the eight changed existing test modules
plus the new regression module exited 0 and collected 209 items. This is
collection evidence only; the real-provider tests were not executed.
`git diff --check` passed before evidence/context finalization.

The isolated `claude auth status --json` probe returned exit 1,
`loggedIn=false`, `authMethod=none`; no real provider invocation was made.
Thus the real-provider test lane remains unverified and must not be reported
as green.

## Shared Docker cleanup fixture incident

One earlier exploratory pytest invocation accidentally loaded the repo-wide
`tests/conftest.py` autouse cleanup, which removes Docker resources with the
shared `wm-` prefix. The contemporaneous Docker inventory did not record the
pre-test names, so it is not possible to prove whether any pre-existing
`wm-` resource was removed. A later read-only `docker events` query timed out
without output and did not resolve that uncertainty. At the time of a later
read-only check, no `wm-` resources were listed. No further pytest command
was run without `--confcutdir=/tmp`; no Docker cleanup command was issued by
this task. This incident is disclosed rather than represented as a clean
shared-runner audit.

## Replay and remaining gates

Set `VENV` to the already existing ProjectFlow test venv; run the six-test
isolated module with the command above from the repository root. For the
affected real-provider modules, use the repository's
normal test environment only after an authorized isolated Claude identity
is provisioned; do not reuse the shared home login or bypass the auth check.
The full suite was not run because the isolated config is unauthenticated
and the repository autouse Docker fixture has broad shared-daemon cleanup.

This milestone does not qualify Gate A (full suite), provider runtime, Nova
approved-reference visual gates B/C, or exact-SHA CI gate E. The PR remains
draft; no CI was enabled or bypassed. Gate F is not cleanly re-certified due
to the disclosed Docker cleanup uncertainty. Rollback is by reverting only
the scoped test-harness commit; no production rollback is applicable.
