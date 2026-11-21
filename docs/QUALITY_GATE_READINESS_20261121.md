# ProjectFlow Quality Gate — integration readiness (21 November 2026)

## Precise current outcome

**Ed25519 staging canary is active. Production Quality Gate is NOT enabled.**

The integration branch combines the latest known `main` (including
Observability view-only demo #59) with the Quality Gate and a new optional
isolated-Docker QA runner. The branch is not merged and no Pull Request was
opened or GitHub Actions workflow launched.

## Verified code and test evidence

- Latest baseline main included: `584e6cef7bce9020a53b9979cf354441907140a7`.
- Quality Gate QA code tested on NEC: `4644b0f8a0de127399196cd7cd3f65f7c016c7ce`.
- Focused cryptographic/HTTP/runner suite: **43 PASS**.
- Targeted Quality Gate and historic merge regression: **89 PASS,
  0 FAIL, 463 dependency warnings in 163.68 seconds**.
- Local Python QA working tree clean; 0 remaining `wm-*` Docker
  test containers/networks.
- New QA image built from `docker/qualitygate-pytest.Dockerfile` with
  `--pull=false`; immutable local image ID:
  `sha256:e2de9828408637b8d9a70fff9d1e7048a3cd0cf32b3b6ab4a920cfc0e5435b25`.
- **The live container-invoked pytest test is NOT verified.** The
  connected command interface blocked this runtime operation. The
  89 test passes include unit/mocked Docker argument contract tests,
  not proof of actual runtime sandbox enforcement.
- Subsequent changes to the branch after tested source SHA were
  **only** `docs/QUALITY_GATE_ISOLATED_QA_20261121.md` and the four
  standalone `demo/observability-view/*` files from `main`.
  No previously tested Quality Gate backend or runner code changed.
- The Observability JavaScript demo was separately run on MacBook
  with Node 22.23.4 from its intended directory:
  `node test-playback.cjs` -> **DEMO_PLAYBACK_TEST_PASS**. NEC itself
  does not currently have Node installed; no runtime was installed
  on that QA node merely to run the demo.

## Staging and secrets

- Ed25519 staging service: `projectflow-qualitygate-ed-stage.service`
  on NEC, loopback port `127.0.0.1:18766`.
- Existing staging policy protects only synthetic task `#1`.
- Valid, missing, stale and tampered signed receipts previously
  confirmed through the staging decision gate.
- The staging app verifier has only a public-key path, no HMAC secret or
  private signing key. The disposable NEC signer key was deleted.
- The old HMAC staging service on port 18765 remains stopped/disabled.
- The **new** `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1` flag
  **must remain unset** until a real containerized signer run has been
  verified. Otherwise a signed host-direct receipt is rightly rejected
  and the selected staging task becomes blocked.
- Ed25519 receipts expire after 24 hours; an expired canary receipt
  requires a new independent QA run before any merge.

## Production promotion blockers

1. Establish a **distinct QA signing principal** with no agent-controlled
   access to its private key; container runner/test code must never
   reach its host or Docker socket.
2. Complete a real isolated-container pytest run and inspect the
   recorded immutable image ID, test logs and resource cleanup.
3. Use an authenticated, authorized path to deliver the **signed receipt
   and public key only** to the verifier. The attempted Mac-to-NEC
   transfer was blocked by the connected command interface; do not
   bypass that control or claim independent transfer succeeded.
4. Arrange GitHub-enforced merge protection. Current `.github/workflows/ci.yml`
   triggers GitHub Actions for `pull_request` and pushes to `main`.
   Do not create a PR/merge simply to check the branch while the
   no-GitHub-Actions requirement applies. The GitHub App does not
   have permission to inspect branch protection details (403).
5. Independent operator approval, canary runtime smoke tests, and
   rollback review before deploying the code to production.

These are concrete security and operational blockers, not failed code
regressions. The current staging Quality Gate continues to operate
independently of them.
