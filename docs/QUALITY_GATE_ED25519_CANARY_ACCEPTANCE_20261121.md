# Quality Gate Ed25519 — completed functional canary (21 Nov 2026)

## Acceptance outcome

**Ed25519 staging canary is ACTIVE and functionally ACCEPTED on NEC.**
This is not a claim of independent production QA signing, production
deployment, or GitHub `main` branch enforcement.

- Service: `projectflow-qualitygate-ed-stage.service` (systemd user,
  configured for autostart), status `active`.
- Local-only listener: `127.0.0.1:18766`.
- Health: `GET /health -> {"status":"ok","auth_mode":"none"}`.
- Staging application SHA:
  `e33d475613b1e8f3480d885f4fc86e1efc8e7d5e`.
- Pilot scope: ProjectFlow staging task ID `1`, repo ID `1`,
  repository name `projectflow-workspace-manager`.
- Pilot test pinned by reviewed task policy:
  `tests/test_harness_monitor_report.py`.
- Test task status: `READY_FOR_MAIN`, reviewer status PASS **in this
  synthetic staging task only**; no real code PR/merge/deployment was
  initiated.
- Feature flag: `PROJECTFLOW_QUALITY_PILOT_TASK_IDS=1`.
- Verifier mode: **Ed25519 public-key-only**. The live web process has
  public-key file path, **no legacy HMAC key and no private signing key**.

## Readback from the live staging integration decision

The following outcomes were exercised on the running NEC staging
instance, not inferred from self-reported agent prose:

| Scenario | Exact observed result |
| --- | --- |
| Valid signed receipt at exact source SHA | Merge blockers: `NO_PR`, no QA blocker |
| Receipt deliberately removed | Merge blockers: `QUALITY_ATTESTATION_MISSING,NO_PR` |
| Expected source SHA differs | `QUALITY_ATTESTATION_STALE` |
| Receipt JSON payload deliberately tampered | `QUALITY_ATTESTATION_INVALID` |
| Original receipt restored | QA verifier returns success |
| Unselected task ID `2` | No Quality Gate blocker |
| Actual GitHub PR absent | Merge remains disallowed: `NO_PR` |

The receipt was restored after negative tests. The demo private key
was deleted after signing; the public PEM and signed JSON remain for
verification until the 24-hour validity window elapses.

**Important:** For the completed live Ed25519 canary, the disposable key
and receipt were generated and signed on NEC. The separate MacBook
signer also passed and generated an Ed25519 receipt for the same source
SHA; transfer of those Mac artifacts was blocked by the connected
command channel. The Mac signer did not supply the receipt currently
used by NEC. In production the QA signing principal, key and test
execution must be independently isolated from the ProjectFlow web
principal and coding agents. The one-user NEC canary does not prove
that isolation.

## Previous test evidence

The integration branch source tree was checked bit-for-bit against
the tested local merged Git tree:
`217d568e4c9b137a6d2c361d338d3ed9a8d76ce2`.

- 85/85 merge and Quality Gate regression tests PASS.
- 257/257 main Observability/Planner/authorization tests PASS.
- 23/23 follow-up Ed25519 signer CLI, old HMAC compatibility and HTTP
  merge tests PASS.
- 0 remaining test Docker `wm-*` containers/networks in acceptance.

## Legacy HMAC pilot retired on NEC

The earlier `projectflow-qualitygate-canary.service` listening on
`127.0.0.1:18765` was **stopped and disabled** after the new Ed25519
canary passed. The legacy symmetric HMAC environment variable was
removed from its private staging EnvironmentFile, reducing unnecessary
secret exposure; old staging data remains intact for audit. NEC
readback:

- HMAC service: `inactive`, `disabled`.
- HMAC environment keys in legacy configuration: `0`.
- New Ed25519 service: `active`.
- Active QA listener: `127.0.0.1:18766`.

## Rollback and next production gates

- To stop new canary without altering production:
  `systemctl --user stop projectflow-qualitygate-ed-stage.service`
- To disable autostart:
  `systemctl --user disable projectflow-qualitygate-ed-stage.service`
- To keep service online but disable the Quality Gate, clear
  `PROJECTFLOW_QUALITY_PILOT_TASK_IDS` in the private Ed staging
  EnvironmentFile, then restart that Ed staging service.
- Missing/stale receipts **intentionally block** the selected task
  after 24 hours; do not silently disable the check to hide expiry.
- GitHub `main` remains separate and protected; this staging instance
  does not block merges made directly on GitHub.
- Before any production rollout, establish an independently protected
  signer principal, authenticated artifact transfer into a verifier-only
  location, operator approval, and GitHub branch protection/rulesets.
- No PR was opened, no GitHub Actions was launched, and no production
  merge/deployment was performed.

This document supersedes the pending-staging-activation snapshot in
`docs/QUALITY_GATE_ED25519_ROLLOUT_20261121.md`.
