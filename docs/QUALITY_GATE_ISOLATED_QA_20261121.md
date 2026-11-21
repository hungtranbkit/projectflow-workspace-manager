# Quality Gate: isolated Docker QA runner — 21 November 2026

## Decision

**Implementation and simulated security tests PASS; a real isolated-container
test invocation has NOT been accepted yet.**

This is a new opt-in hardening layer added to the existing signed Ed25519
Quality Gate. It does **not** change the production code or automatically
enable stricter enforcement on the running staging instance.

## Actual repository and QA evidence

- Source branch: `integrate/quality-gate-main-20261121`
- QA source commit: `4644b0f8a0de127399196cd7cd3f65f7c016c7ce`
- QA test node: NEC Linux, Docker 29.1.3, local Python 3.14 virtualenv
- Local-only image build from `docker/qualitygate-pytest.Dockerfile`: **PASS**
- Image tag used for this build: `projectflow-qa-pinned:20261121`
- Immutable image ID:
  `sha256:e2de9828408637b8d9a70fff9d1e7048a3cd0cf32b3b6ab4a920cfc0e5435b25`
- Focused Quality Gate + new isolation tests:
  **43 passed, 67 warnings**
- Combined previous merge regression and Quality Gate tests:
  **89 passed, 463 warnings in 163.68 seconds**, exit code 0
- Working tree after QA: clean.
- Remaining `wm-*` test containers: 0. No Docker test networks left.

These results prove integration and the **argument/snapshot contract** of the
new isolated runner. They do not prove that a real pytest run inside the new
Docker image succeeded. The current connected command channel blocked that
specific runtime invocation. Do not report the container isolation as
end-to-end accepted.

## Implemented security contract

The new `scripts/agent_quality_isolated_runner.py`:

- Accepts only a locally available, immutable **sha256 image ID**.
- Exports a clean `git archive HEAD` snapshot into a disposable directory;
  never mounts `.git`, untracked files, the agent workspace directly, or
  the independent QA signer's private key.
- Verifies the approved `tests/test_*.py` selector, already pinned by task
  policy; no agent-supplied shell command or test override.
- Creates the container with `--pull=never`, `--network=none`,
  `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`,
  `--user=65534:65534`, PID/CPU/RAM limits and a bounded `/tmp` tmpfs.
- Mounts the test snapshot as `/work` **read-only**. No Docker socket.
- Passes only `HOME=/tmp`, `PYTHONDONTWRITEBYTECODE=1` and
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` to the container.
- Limits captured stdout/stderr and attempts to stop an owned Docker
  container if the CLI times out; no global Docker pruning.
- Signs only after the test runner returns PASS and source SHA is unchanged.

The signed receipt now records `qa_isolation` and `qa_image_id`.
When the verifier has `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1`, it
rejects otherwise-valid host-direct evidence and invalid image IDs.
There is no fallback to HMAC or host pytest in this required mode.

## Controlled runtime qualification (not yet completed)

On an authorized, separately controlled QA machine with Docker installed,
build the reviewed Dockerfile locally:

```bash
docker build --pull=false -t projectflow-qa-pinned:20261121 \
  -f docker/qualitygate-pytest.Dockerfile .
docker image inspect --format '{{.Id}}' projectflow-qa-pinned:20261121
```

From an independently controlled **signer** (a different OS principal or a
separate trusted service), set the image ID and make isolated QA mandatory
before signing. Store the Ed25519 **private** key outside both worktrees and
outside any mounted Docker directory:

```bash
export PROJECTFLOW_QUALITY_PILOT_CONTAINER_IMAGE_ID='sha256:<verified image ID>'
export PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1
export PROJECTFLOW_QUALITY_ATTESTATION_PRIVATE_KEY_FILE='/protected/qa/private.pem'
python scripts/agent_quality_sign_pilot.py \
  --worktree /isolated/candidate-at-exact-commit \
  --commit EXACT_SOURCE_SHA \
  --task-id ACTUAL_STAGING_TASK_ID \
  --repository-id ACTUAL_STAGING_REPOSITORY_ID \
  --repository-name projectflow-workspace-manager \
  --receipt-dir /trusted/receipt-handoff
```

Transport only the signed receipt and matching public key through an
approved authenticated handoff. Set the verifier's
`PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1` **only after** one real
isolated signer run passes; then verify accepted exact SHA, rejected stale
SHA, missing evidence, tampering, and rejection of signed host-direct
evidence on the live staging merge route.

## Existing stages / rollback / production limits

- NEC Ed25519 staging still listens on `127.0.0.1:18766`. Its existing
  synthetic canary task #1 is scoped to the old attestation proof; the
  new required-isolation flag must remain unset until runtime
  qualification completes.
- The former HMAC staging service on port 18765 was stopped and disabled.
- GitHub `main` was not changed, no PR was opened, and no GitHub Actions
  job was deliberately triggered.
- Current GitHub CI workflow includes both `pull_request` and
  `push: [main]`. A PR or main merge would trigger Actions; avoid them
  while the requested no-GitHub-Actions policy remains.
- Connected GitHub App access cannot inspect the branch-protection detail
  endpoint (403). Do not infer that it enforces signed QA. An external
  GitHub merge bypasses ProjectFlow's app-level gate until a separately
  managed ruleset/check is in place.
- Running Docker tests as the same OS account as an agent with Docker
  socket access is not a complete trust boundary: separate users,
  daemon access, secret stores and the approved receipt inbox are still
  required for production.

**Rollout gate:** do not promote the new Docker-required flag or claim
production completion without real container evidence and independently
protected signer credentials.
