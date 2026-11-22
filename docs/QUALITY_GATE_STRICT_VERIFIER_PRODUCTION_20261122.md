# Quality Gate strict verifier — real Dell release and NEC shadow acceptance (22 Nov 2026)

## Release outcome

**Code hardening DEPLOYED on Dell Workspace Manager. Mandatory signed QA enforcement remains OFF on production because no independently signed production receipt exists.**

The strict verifier rejects legacy HMAC, unsigned/host-direct QA proofs and
Docker image IDs that are not approved by the verifier's independently set
`PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID`. The key and image setting are
configured on the verifier host, not sourced from task/agent JSON.

- Code commit: `26c1d3c45322818d30ddff1948684e6be3d095fd`.
- Source branch: `integrate/quality-gate-main-20261121` (NO PR opened, NO
  merge to GitHub `main`, NO GitHub Actions workflow triggered).
- Changed production backend:
  `app/services/quality_attestation_pilot.py`, with focused regression
  additions in `tests/test_quality_isolated_runner.py`.
- Previous Dell release (retained for rollback):
  `/home/dell/workspace/projectflow-qualitygate-release-20261122`,
  commit `69b7e4405d9275f71f4b46a3f7dd95a02a41ce24`.
- New Dell release:
  `/home/dell/workspace/projectflow-qualitygate-release-20261122-strict`
  at commit `26c1d3c`.
- After restarting `workspace-manager.service`, systemd reported
  `active`, the process CWD pointed to the new release, and
  `/health`, `/api/tasks`, `/api/repositories` all returned HTTP 200.
- All **7/7** production `/api/tasks/{id}/integration-readiness` GETs
  returned HTTP 200. Production SQLite `PRAGMA quick_check=ok`, 7 tasks,
  5 registered repositories. Approximately 138 GB available on Dell.
- The runtime environment remains
  `PROJECTFLOW_QUALITY_PILOT_TASK_IDS=` (empty) and
  `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=0`.
  **No production merge is newly protected by these feature flags.**

### Backups and rollback

The pre-upgrade systemd drop-in and SQLite snapshot are at:

`/home/dell/workspace/projectflow-qualitygate-release-20261122-strict/rollback-before-strict-20261122/`

Snapshot quick check was `ok`. Roll back **only code** by restoring
`workspace-manager-dropin.conf` to
`~/.config/systemd/user/workspace-manager.service.d/90-qualitygate-release.conf`,
running `systemctl --user daemon-reload` and restarting
`workspace-manager.service`. Verify process CWD and `/health`.
Do not blindly restore an old DB over newer production writes.

## New fail-closed strict-mode requirements

When `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1` and a task is selected by
`PROJECTFLOW_QUALITY_PILOT_TASK_IDS`, the verifier now requires **all**:

1. `PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE` with an authorized,
   existing, non-symlink Ed25519 **public** key (no signing key in web process).
2. `PROJECTFLOW_QUALITY_ATTESTATION_KEY` must be unset — legacy shared-secret
   HMAC is not an acceptable fallback in strict mode.
3. `PROJECTFLOW_QUALITY_APPROVED_QA_IMAGE_ID=sha256:<64 lowercase hex>`,
   configured on the verifier independently of the signer report.
4. The **signed** receipt must carry
   `qa_isolation=DOCKER_READ_ONLY_NO_NETWORK` and `qa_image_id` exactly
   equal to the verifier-approved image ID.
5. The existing exact task/repo/source commit, expected test policy, clean
   source, lint/test PASS, signature and <=24h freshness checks still apply.

Missing/malformed verifier configuration returns
`QUALITY_ATTESTATION_CONFIG`; a receipt asserting another Docker image
returns `QUALITY_ATTESTATION_STALE`; missing/incorrect isolation evidence
returns `QUALITY_ATTESTATION_INVALID`. No downgrade path is added.

## Real testing

- Dell focused verifier security tests: **27 passed**, 67 upstream warnings,
  7.25 seconds, exit 0.
- Dell extended merge/integration/strict quality tests: **90 passed**, 749
  upstream warnings, 121.62 seconds, exit 0. No lingering `wm-*` Docker
  containers.
- NEC explicitly invoked
  `tests/test_quality_isolated_runtime.py` against the **real pinned Docker
  image** `sha256:e2de9828408637b8d9a70fff9d1e7048a3cd0cf32b3b6ab4a920cfc0e5435b25`
  from the clean code checkout of commit `26c1d3c`: **1 passed**, 12
  dependency warnings, 12.16 seconds, exit 0. The nested real pytest test
  executes the approved monitor-report test with a readonly, offline,
  non-root, bounded tmpfs container.
- Green test against a **copied Dell production DB** and the new code:
  **10/10 endpoints HTTP 200**, no live DB changes.
- Tests include strict-mode rejection for absent image pin, mutable tag,
  mismatched approved image, old host-direct proof and legacy HMAC receipt.
  A signed isolated receipt with the approved image is accepted **in
  isolated unit tests using ephemeral test fixtures**, **not** evidence
  that an independent production principal has signed it.

## Independent strict shadow on NEC — NOT production

A separate user service
`projectflow-qualitygate-strict-shadow-20261122.service` runs at
`127.0.0.1:18769` against a **copied** staging database, repository,
old public key and old signed receipt under
`/home/rag/workspace/qualitygate-strict-shadow-20261122`.

- Source checkout: `/home/rag/workspace/qg-strict-isolation-20261122`,
  commit `26c1d3c`.
- Running service: `active`, `/health` OK.
- Strict flag `1`, approved pinned Docker image ID set on verifier.
- Existing **host-direct** Ed25519 canary receipt was rejected as
  `QUALITY_ATTESTATION_INVALID` (expected). An unrelated task was
  unaffected.
- Old staging service `projectflow-qualitygate-ed-stage.service` at
  `127.0.0.1:18766` remains active/unchanged.
- No private signing key or new signed independent QA receipt was created
  or transferred by this deployment. A healthy shadow HTTP endpoint does
  **not** imply a merge is allowed.

## Remaining P0 work to complete independent production enforcement

- Assign a genuinely separated and authorized QA principal and review its
  private-key custody and least-privilege controls. Do not pass a signing
  key via an agent shell, source tree, task input or app environment.
- QA principal checks an exact clean commit, runs the approved immutable
  Docker test suite, signs only if PASS and publishes **only** the signed
  receipt plus approved public key over an authenticated channel.
- Verifier operator pins that QA image ID and public key, and selects
  a real reviewed task/repository policy. Show one real blocked and one
  real accepted merge decision with independent evidence.
- Review GitHub rulesets/required checks using appropriate repository
  administration permissions. Application-only gates do not prevent direct
  GitHub merges. The current workflow is still configured to trigger
  Actions on PRs and main pushes, so no PR or main push was initiated.
- Add trustworthy expiry/renewal telemetry and real production KPI
  tracking for signed QA adoption and blocked merge attempts.

**DoD:** Current result is **strict verifier READY on a real NEC shadow and
new Dell code deployed**; **independent signed production merge gate remains
blocked**. Do not claim 100% or production strict enforcement yet.
