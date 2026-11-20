# Agent quality gates — isolated pilot (2026-11-20)

This pilot tests the three ideas from the video without changing existing
ProjectFlow production behavior or enabling GitHub Actions. It is **opt-in**:
there is no automatic merge, no auto-deploy, and no change to existing CI.

## Three enforceable controls

1. **A lint rule instead of another reminder.** A Python AST scan rejects
   direct `eval`/`exec`, `os.system`, `subprocess` with literal `shell=True`,
   and silent `except: pass`. Comments and strings do not trigger a match.
   Scope: agent-changed `.py` files passed via `--file`. Never apply these
   rules blindly to all historical code.
2. **Narrow, reviewed prohibitions.** Only patterns with explicit security/
   reliability rationale are blocked. `useEffect` is not blanket-banned:
   it can be required for valid React side effects. Other rules need a
   real bug, reviewer approval, a negative test, and a safe alternative.
3. **Proof produced by a runner, not by the agent's prose.** The command
   executes the **fixed** pilot pytest suite locally and records its
   exit status, output log, timestamp, git HEAD, and lint result in JSON.
   For UI changes, pass `--require-ui --ui-evidence artifacts/path.png`
   (or a video). A real file is required; an empty report is not proof.

## Run locally from the repository root

```bash
python3 -m pytest -q tests/test_agent_quality_pilot.py
python3 scripts/agent_quality_pilot.py \
  --task-id QA-PILOT-001 \
  --file scripts/agent_quality_pilot.py
# Expected: PASS, with artifacts/agent-quality-pilot/QA-PILOT-001.{json,log}

# A UI task without a screenshot or video is intentionally blocked:
python3 scripts/agent_quality_pilot.py \
  --task-id QA-PILOT-UI-MISSING \
  --file scripts/agent_quality_pilot.py --require-ui
# Expected: FAIL
```

## Acceptance limits / rollout

- A `PASS` proves **only** the targeted lint rules and fixed pilot tests ran.
  It **does not** prove a feature works, validate the UI contents, or grant
  merge/deploy permission. Keep independent reviewer and real-device QA.
- The current fixed test command is a **self-test** for the pilot itself.
  To promote this to real feature gates, bind repo/feature-specific tests
  and required evidence types to a trusted, reviewed policy, use a clean
  SHA-isolated worktree, and have a separate verifier replay the tests.
- Do not make `AGENTS.md` longer as a substitute for these checks. Apply
  gates to one real project task only after this pilot is reviewed.
- No GitHub Actions: run through local ProjectFlow/Terminal MCP pre-merge.

Sample report schema: `status`, `task_id`, `generated_at_utc`, `scope_files`,
`lint_violations`, `test_command`, `test_exit_code`, `test_log`,
`ui_required`, `ui_evidence`, `git_head`, `failure_reasons`, `limitations`.


## Task-scoped rollout test (real ProjectFlow module)

The first runner above is only a self-test. For a **real** repository task,
the opt-in task runner reads the reviewed, version-controlled policy
`config/agent_quality_tasks_pilot.json`; the coding agent cannot override
the test suite, source files or UI requirement with CLI flags. The policy
pins these example tasks:

- `PROJECTFLOW-MONITOR-REPORT-PILOT`: checks
  `scripts/harness_monitor_report.py` against the **real**
  `tests/test_harness_monitor_report.py` regression suite.
- `PROJECTFLOW-QUALITY-SELFTEST`: checks the original pilot rules and its tests.
- `PROJECTFLOW-UI-NEGATIVE-PILOT`: intentionally fails without a real
  screenshot/video, even when the agent does not request UI verification.

Run on a separate Linux worktree or a disposable checkout:

```bash
python3 -m pytest -q tests/test_agent_quality_pilot.py tests/test_agent_quality_task_gate.py
python3 scripts/agent_quality_task_gate.py --task-id PROJECTFLOW-MONITOR-REPORT-PILOT
python3 scripts/agent_quality_task_gate.py --task-id PROJECTFLOW-UI-NEGATIVE-PILOT
```

A PASS writes a machine-generated JSON/log pair under
`artifacts/agent-quality-pilot/`, including pinned tests, exit code,
commit SHA, per-file SHA256 digests, policy hash, worktree dirty flag,
and failure reasons. **A failed lint rule, failed test, missing test
file, unknown task, or missing required UI proof must block the gate.**

This is **not** a new approval or merge mechanism. A source proof is
not equivalent to real-device UI evidence, independent reviewer
acceptance, or production smoke tests. Do not open a PR while the
repository's `.github/workflows/ci.yml` still runs GitHub Actions on
all pull requests; branch-only work avoids this trigger.
