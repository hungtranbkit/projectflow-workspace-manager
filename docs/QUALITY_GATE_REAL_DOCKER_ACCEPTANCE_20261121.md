# Quality Gate — Real Docker QA acceptance (2026-11-21)

## Verdict

**PASS — real isolated Docker pytest runtime and targeted integration regressions.**

**NOT YET ACCEPTED — independent signing authority, authenticated signed-receipt
handoff, production branch protection and production deployment.**

This result resolves the previous gap where a Docker image had been built
and Docker argv tested but no actual pinned test suite had run inside the
restricted container. It does NOT imply that a valid private-key signature
was generated in this acceptance turn.

## Precise verified source and image

- Git repository: `hungtranbkit/projectflow-workspace-manager`
- Branch: `integrate/quality-gate-main-20261121`
- **Exact QA code commit:** `2d23d8eafedbbeefe5c72b8b0445520819243ca6`
- QA host: NEC Linux, Docker 29.1.3, Python 3.14 host venv
- Container runtime: Python 3.12.16, pytest 9.2.0
- Immutable locally built QA image:
  `sha256:e2de9828408637b8d9a70fff9d1e7048a3cd0cf32b3b6ab4a920cfc0e5435b25`
- Candidate source: a clean checkout at the commit above, exported by
  `git archive HEAD` into a read-only mount (not the live source tree).
- No network or Docker socket is exposed to the test container.

## Actual runtime evidence and regression resolution

An initial true Docker run **FAILED** at pytest collection because
importing `app.main` initialized SQLite at the default
`/work/data/workspace-manager.db`; `/work` was mounted read-only.

Fix in `scripts/agent_quality_isolated_runner.py`: set these
container-only runtime paths under bounded `/tmp` tmpfs:

```text
WORKSPACE_MANAGER_ROOT=/tmp
WORKSPACE_MANAGER_DB=/tmp/qg-state/workspace-manager.db
WORKSPACE_MANAGER_STATE_DIR=/tmp/qg-state/runtime
WORKSPACE_MANAGER_WORKTREE_ROOT=/tmp/qg-worktrees
```

No permission widening, source writable mount, network access, privileged
container, or Docker socket was introduced.

The exact pinned test `tests/test_harness_monitor_report.py` subsequently
ran in the Docker container with **exit code 0**. Pytest emits ten passing
dots and `[100%]` because the repository already adds `-q` and the
runner also passes `-q`. A dedicated opt-in real runtime test,
`tests/test_quality_isolated_runtime.py`, validates this output shape
rather than assuming a literal `10 passed` footer. It is skipped
unless the operator explicitly supplies `PROJECTFLOW_QA_RUNTIME_IMAGE_ID`.

The source-level `tests/test_quality_isolated_runner.py` additionally
asserts strict container arguments, lack of `.git` and untracked files,
absence of signing material and the four new tmpfs runtime paths.

### Final combined QA

Exact command scope:

```bash
PROJECTFLOW_QA_RUNTIME_IMAGE_ID='sha256:e2de9828408637b8d9a70fff9d1e7048a3cd0cf32b3b6ab4a920cfc0e5435b25' \
  python -m pytest -q -o addopts='' \
    tests/test_real_merge.py \
    tests/test_merge_reconciliation.py \
    tests/test_integration_push.py \
    tests/test_integration_create_idempotent.py \
    tests/test_quality_attestation_http_e2e.py \
    tests/test_quality_attestation_pilot.py \
    tests/test_quality_ed25519_pilot.py \
    tests/test_agent_quality_pilot.py \
    tests/test_agent_quality_task_gate.py \
    tests/test_harness_monitor_report.py \
    tests/test_quality_signer_cli.py \
    tests/test_quality_isolated_runner.py \
    tests/test_quality_isolated_runtime.py
```

Observed machine log:

```text
90 passed, 463 warnings in 143.69s (0:02:23)
exit code: 0
source HEAD: 2d23d8eafedbbeefe5c72b8b0445520819243ca6
working tree: clean
wm-* containers: 0
wm-* networks: 0
projectflow-qa containers: 0
```

Machine-local log:
`/tmp/qg-full-runtime-acceptance-20261121.log`.
The 463 warnings are non-failing third-party deprecations and should be
tracked in dependency maintenance.

## Ed25519 signature/handoff status

The next attempt to generate a new signed receipt using an ephemeral
private Ed25519 key and `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1` was
**blocked by the connected command safety layer**. It was not signed,
and no new receipt was delivered or activated on staging. The ephemeral
test private key created for this attempt was deleted; no new signed
receipt was left by that attempt.

The existing earlier Ed25519 NEC staging canary is a separate,
host-direct signing demonstration. It is still running on the synthetic
task and must not be relabeled as cryptographic proof that this newer
Docker-runner attestation was signed.

**For a true independent QA trust boundary:** use a signer principal
with protected private key; execute candidate tests in a sandbox with
no signer filesystem/credentials or Docker socket; check exact SHA and
reviewed test policy; sign only after success; securely deliver just
the public key and signed JSON receipt to a verifier-only location;
then require the isolation flag on the staging ProjectFlow service and
prove tamper/missing/stale behavior again.

## Production / rollback status

- Existing Ed25519 staging canary on NEC:
  `projectflow-qualitygate-ed-stage.service` at
  `http://127.0.0.1:18766` (loopback only), health OK.
- Its app source remains the prior accepted
  `e33d475613b1e8f3480d885f4fc86e1efc8e7d5e`; this was not
  silently redeployed to the newer QA commit.
- `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1` remains **OFF** in
  the staging service pending independently signed runtime proof.
- Legacy HMAC staging service is inactive.
- GitHub `main` unchanged by this acceptance. No new PR, production
  deploy or GitHub Actions invocation.
- GitHub PRs or pushes to `main` would trigger the existing
  GitHub Actions CI workflow. The in-app Quality Gate cannot prevent
  a direct GitHub merge without separately managed GitHub rulesets.

**Final classification:** Real-Docker runtime acceptance **PASS**;
production rollout and independent signed attestation **BLOCKED pending
authorization/security boundary and successful handoff**.
