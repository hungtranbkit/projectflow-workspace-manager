# Independent pilot QA — 2026-11-20

## Scope and provenance

- Repository: `hungtranbkit/projectflow-workspace-manager`
- Experiment branch: `experiment/agent-quality-proof-gate-20261120`
- Exact verified source commit: `ac0962da02eadb01accf9efdd19093437d8029a5`
- Independent executor: `m910` (ThinkCentre Linux), isolated repository under `/tmp/projectflow-quality-pilot-20261120`
- Isolated test environment: Python 3.14.4 venv under `/tmp/projectflow-quality-venv-20261120`, project installed with `pip install -e '.[test]'`
- Quality policy: `config/agent_quality_tasks_pilot.json`
- Production code, main branch, deployments and live services: **unchanged**; no PR created or GitHub Actions requested.

## Measured outcomes (from Terminal MCP shell readback, not an agent self-report)

| Scenario | Scope | Observed status | Underlying test exit |
| --- | --- | --- | --- |
| New gate unit suite | `tests/test_agent_quality_pilot.py` + `tests/test_agent_quality_task_gate.py` | **16 passed**, 12 warnings, 3.14s | 0 |
| Clean source | Real `scripts/harness_monitor_report.py` against `tests/test_harness_monitor_report.py` | **PASS**; `scope_dirty=false` | 0 |
| Deliberate dangerous Python API | Temporary `eval()` added to disposable checkout, then restored | **FAIL**, one lint violation | Tests not run |
| Deliberate functional regression | Temporary wrong `percent_bar()` in disposable checkout, then restored | **FAIL**, zero lint violations | 1 |
| UI proof omitted | Policy task `PROJECTFLOW-UI-NEGATIVE-PILOT` | **FAIL** with `UI change needs a real screenshot/video` | Tests not run |

Observed clean-state machine receipt:
```text
FINAL_PROOF PASS 0 False ac0962da02ea tests/test_harness_monitor_report.py
UNIT_FINAL_EXIT=0
16 passed, 12 warnings in 3.14s
```

The temporary source mutations were removed after each test. The final `git status --short` showed no tracked or untracked changes.

## Where local machine evidence was written

- Clean: `/tmp/quality-proof-m910-final/PROJECTFLOW-MONITOR-REPORT-PILOT.json`
- Lint failure: `/tmp/quality-proof-m910-lint/PROJECTFLOW-MONITOR-REPORT-PILOT.json`
- Regression failure: `/tmp/quality-proof-m910-test-fail/PROJECTFLOW-MONITOR-REPORT-PILOT.json`
- UI evidence failure: `/tmp/quality-proof-m910-ui/PROJECTFLOW-UI-NEGATIVE-PILOT.json`
- Unit test log: `/tmp/quality-final-16.log`

These are **machine-local, temporary files**; this checked-in document is the durable QA summary, not a substitute for the original logs.

## Acceptance boundary

**Pilot QA PASS; not production acceptance.** No automatic merge gate has been installed. The pilot requires explicit local invocation and uses policy-pinned tests, but the policy and runner share a checkout, so promotion needs a trusted independent verifier and SHA-isolated execution. UI proof validation checks file existence and headers, not visual correctness. Retain separate reviewer approval, real-device E2E verification and deployment checks before broad rollout.
