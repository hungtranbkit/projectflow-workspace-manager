# Signed pre-merge QA gate — independent test record (20 November 2026)

## Experiment identity

- Branch: `experiment/agent-quality-proof-gate-20261120`.
- Final test checkout SHA: `e13f0529dc75f8c0200ece35b903f0010b904867`.
- Runner: M910 Linux (ThinkCentre), isolated checkout `/tmp/projectflow-quality-pilot-20261120`.
- Python 3.14.4, pytest 9.2.0, isolated virtualenv.
- Changes live only on experiment branch. No main merge, production deployment, GitHub PR or GitHub Actions run was initiated by this experiment.

## Observed PASS results

- `tests/test_agent_quality_pilot.py` +
  `tests/test_agent_quality_task_gate.py` +
  `tests/test_quality_attestation_pilot.py`:
  **29 passed, 34 warnings, 7.94s**, exit code **0**, on final SHA.
- The tests cover default-disabled behavior, exact task/repository/SHA binding,
  HMAC tamper rejection, 24-hour expiry, absent key/receipt, dirty checkout,
  signer wrong-SHA rejection, and actual FastAPI routes:
  `/mark-merged` and `/confirm-external-merge` block bypass attempts.
- End-to-end signer pilot previously executed against SHA
  `d6352a3e04ff2b32653356170c84aed13c3fd79b`:
  `ATTESTED` from real pinned
  `tests/test_harness_monitor_report.py`; separate live verification
  returned **VERIFY_SIGNED PASS** and
  **VERIFY_WRONG_SHA QUALITY_ATTESTATION_STALE**. This is explicitly
  a disposable proof with an ephemeral test key, **not** a production
  QA key or an attestation for the final SHA.
- The earlier quality-gate pilot showed a clean module `PASS`,
  deliberate lint error `FAIL`, deliberate functional regression
  `FAIL`, and missing UI proof `FAIL`.

## Legacy regression evidence and blockers

- On the M910 checkout, 36 historical integration/merge regression tests
  ran: **34 passed, 2 failed** in 108.8 seconds.
- Failure `test_multi_repo_partial_merge_stays_active_not_done` was
  an **environmental Python namespace collision**:
  `from tests.conftest` resolved Terminal MCP's cached package instead
  of this repository. A disposable local `tests/__init__.py` resolved
  the collision and the same test then **passed (1 passed, 3.16s)**.
  `tests/__init__.py` was subsequently added to this experiment branch.
- Failure `test_done_ui_shows_verification_sandbox_action_when_supported`
  expected a running Docker sandbox to become HEALTHY but got UNKNOWN.
  Read-only `docker info` on M910 returned
  `permission denied while trying to connect to the docker API at
  unix:///var/run/docker.sock`. This remains **UNVERIFIED**; we did not
  change Docker permissions, bypass the test, or claim a green regression
  suite.
- An isolated or authorized Docker-enabled QA environment is required to
  rerun that sandbox test before production rollout.

## Security and applicability

- ProjectFlow's **existing** `TaskDecisionService.merge_gate_status`
  checks the signed receipt for selected numeric task IDs and repository
  names; the real GitHub merge route already consumes that method.
- `IntegrationService.integrate_task` checks the same receipt for direct
  local updates and checks that the source SHA has not moved.
- Legacy manual `Mark Merged` is disabled for selected tasks; manual
  `Confirm External Merge` requires the receipt plus existing Git
  ancestry check. Without opt-in environment settings, those paths keep
  existing behavior.
- This is **not** global merge protection. A PR merged directly on GitHub
  can bypass ProjectFlow; enforce GitHub rulesets/branch protection before
  claiming organization-wide controls.
- The signing key must be held only by a separately authorized QA process
  and the verifier. Same-user agent and QA processes are not a strong
  trust boundary. Store receipts outside agent-writable checkouts.
- The pinned policy tests only the ProjectFlow monitor-report module;
  it does not certify other modules, UI behavior, deployment, or user
  acceptance.

## Next operational step

Select a real numeric task/repository pair in a non-production
ProjectFlow instance, establish isolated QA/verifier identities and a
private receipt directory, then enable the opt-in flags as documented in
`docs/AGENT_QUALITY_SIGNED_MERGE_PILOT.md`. Check one valid and one
forged/stale receipt through the **real merge UI** without completing a
production merge. Do not activate the flag broadly while the Docker
sandbox regression remains unverified.
