# Harness MVP — verified staging runbook

## Locked objective

Run one atomic child task: dispatch -> prove agent execution -> independently
accept the result -> safely clean resources. Main goal cannot be silently
changed. A parent may dispatch the next child only when the previous one has
passed its acceptance and cleanup gates.

## Dell Linux acceptance, 15 November 2026

- Real Codex CLI read-only smoke: DONE_VERIFIED, CLEANED_VERIFIED.
- Managed user-systemd Codex task #6: DONE_VERIFIED, CLEANED_VERIFIED.
- Managed Code task #7: Codex fixed calc.add, existing pytest passed, local
  commit dc016a7 on isolated harness/pilot branch, cleanup verified. Codex
  generated cache/PROJECT_CONTEXT.md was archived with hashes, then removed.
- Hard TTL enforcement: systemd RuntimeMaxSec with KillMode=control-group.
  A 20-second sleep limited to 2 seconds ended with Result=timeout, MainPID=0.
  Managed sleep task #5: EXPIRED and CLEANED_VERIFIED, unit Result=timeout.
- Parent goal pilot: child #8 and #9 both DONE_VERIFIED / CLEANED_VERIFIED.
  Parent DONE_VERIFIED only after independent final pytest (1 passed).
- Python unit tests: 33 PASS after immutable parent-plan lock was introduced.
  A full-project regression run is a separate check, not implied here.

## Safety and execution

- scripts/harness_managed_exec.py starts only owned child units registered in
  the SQLite database; task time limits are bounded by the remaining absolute
  deadline and never exceed 24 hours.
- scripts/harness_parent.py registers pre-approved plans and dispatches at
  most one missing child. Goals are locked by SHA-256 plan digests. Expired
  children return BLOCKED_RESCOPE and are never resubmitted unchanged.
- scripts/harness_reconcile.py is the checkpoint and cleanup path. It is
  invoked by an active staging user-systemd timer on Dell every five minutes.
- Coding runs are restricted to clean isolated Git branches named harness/*,
  one existing pytest acceptance file, and Codex workspace-write sandbox.
  Success requires test exit 0, a tracked patch, a local commit and readback.
  The worker does not push, merge or deploy any code.
- Unrecognized, dirty or task-owned resources are not removed automatically.
  Whitelisted cache and generated PROJECT_CONTEXT.md are archived with SHA
  evidence before removal. Arbitrary new files are not deleted.

## Dell staging commands

~~~bash
cd /home/dell/workspace/harness-mvp-impl-20261115
PYTHONPATH=. /home/dell/workspace/projectflow-workspace-manager/.venv/bin/python -m pytest -q tests/test_agent_harness_mvp.py tests/test_agent_harness_terminal.py tests/test_agent_harness_reconcile_cli.py tests/test_agent_harness_managed.py tests/test_agent_harness_parent.py

python scripts/harness_managed_exec.py status
python scripts/harness_parent.py status
python scripts/harness_reconcile.py
bash scripts/harness_staging_timer.sh status
systemctl --user start projectflow-harness-reconcile-staging.service
~~~

To uninstall the staging timer without deleting evidence, use
bash scripts/harness_staging_timer.sh remove.

## Parent JSON example

~~~json
{
  "plan_id": "example-parent-001",
  "main_goal": "Prove one isolated calculator task",
  "enabled": true,
  "steps": [
    {
      "goal": "Produce one verifiable marker",
      "executor": "echo-probe",
      "expected": "MARKER_1",
      "repo": "/absolute/path/to/clean/isolated-git-repo",
      "ttl_seconds": 600
    }
  ],
  "final_acceptance": {
    "repo": "/absolute/path/to/clean/isolated-git-repo",
    "test": "tests/test_existing_acceptance.py"
  }
}
~~~

Install once with:
python scripts/harness_parent.py install --plan-file /path/to/plan.json

The parent requires an independent pytest final acceptance and never infers
main-goal success merely because a child completed.

## Codex coding-task scope guard

Before making an automatic local commit, the Harness independently checks the
real Git delta. Only modified tracked Python implementation files are allowed;
changes to tests, conftest.py, setup.py, configuration or unknown untracked
outputs are refused. An agent's assertion that tests pass is never sufficient.
The acceptance test must already exist in the isolated repository and must
remain unchanged. Known context/cache outputs are archived separately rather
than committed. On scope refusal, the worktree stays for review; no automatic
push, merge or deletion of unknown files takes place.

This guard is a staging safety check, not a substitute for separate code review
or a full regression run.

## Limits

- This is a verified staging Harness, not yet connected to the three project
  ChatGPT monitoring automations, and not a multi-node continuous delivery
  platform. A ChatGPT monitor must first author a safe atomic parent plan.
- The local tool does not autonomously invent and approve new goals from
  natural language, create ChatGPT Automations, or bypass owner consent.
- Hard TTL is enforced only for managed child systemd units; it does not
  impose termination on arbitrary pre-existing Claude/Codex sessions.
- No automatic payment, publishing, credential use, Meta actions, production
  deployment or main branch merge.
- A plan lock prevents accidental drift; an operator with write permission
  to both plan and lock still controls the data.

Tracked in https://github.com/hungtranbkit/projectflow-workspace-manager/issues/39
and https://github.com/hungtranbkit/projectflow-workspace-manager/pull/40.
