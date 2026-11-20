# Quality Gate Ed25519 — integration and staging report (21 Nov 2026)

## Overall status

**Code integration QA PASSED; Ed25519 staging verifier is deployed but NOT YET ENABLED. Production remains unchanged.**

The integration branch is based on ProjectFlow `main` commit
`d4c43168b9945a841b70cf53562aff9c022f3ec3`; it retains the
Observability and Planner fixes, adds the scoped Quality Gate, and
corrects the two test import paths needed when `tests/` is a package.

- Verified integration **source tree**:
  `217d568e4c9b137a6d2c361d338d3ed9a8d76ce2`
- Published integrated code commit:
  `e33d475613b1e8f3480d885f4fc86e1efc8e7d5e`
- Follow-up signer CLI import fix and regression test commit:
  `8935454afff1ac258929324e6aa6e292c0932ef2`
- Both commits exist on
  `integrate/quality-gate-main-20261121`. No PR, main update, or
  GitHub Actions execution was made by this workflow.

## Verified test results

- On NEC Linux, **85/85** targeted Quality Gate and legacy merge
  regression tests passed with zero residual `wm-*` containers/networks.
- On the same merged source tree, **257/257** current main
  Observability/Claude/authorization tests passed, following two
  import-only compatibility fixes.
- On the follow-up signer CLI commit, **23/23** focused signer,
  Ed25519, HMAC-compatibility and HTTP merge tests passed.
- Remote Git tree SHA exactly matched the source tree tested locally,
  and its `app/main.py` Git blob SHA matched
  `c8f1a04bbf344a5fae942a2f442c158fbb684392`.
- Standalone signer CLI was tested with Python isolated import mode
  (`python -I .../agent_quality_sign_pilot.py --help`) and now works
  independently of the current shell directory.

## Ed25519 stage

A **second, isolated staging service** on NEC is running:

- systemd user service: `projectflow-qualitygate-ed-stage.service`
- loopback URL: `http://127.0.0.1:18766`
- health: `{"status":"ok","auth_mode":"none"}`
- source commit deployed: `e33d475613b1e8f3480d885f4fc86e1efc8e7d5e`
- isolated staging database and repository, synthetic task #1, repo #1
- `PROJECTFLOW_QUALITY_PILOT_TASK_IDS` intentionally **empty** at
  report time; no Ed25519 source gating is falsely claimed.

On MacBook Linux, a separate trusted signer checkout running the fixed
CLI executed the pinned real module test and produced an **ATTESTED**
Ed25519 receipt for exactly that staging task/repository/source SHA.
Both the signed JSON receipt and the matching public PEM exist on the
Mac QA host; the one-time Ed25519 private PEM was **deleted** after
signing. The verifier machine does **not** hold that private key.

### Pending artifact delivery

The connected command channel did **not** complete transport of the
public PEM and signed receipt to NEC. Therefore, **do not set**
`PROJECTFLOW_QUALITY_PILOT_TASK_IDS=1` yet and do not label the
asymmetric canary ACTIVE.

An authorized administrator may transfer **only these two non-secret
artifacts** over an authenticated SSH/SCP or other approved device
transport to the staging verifier's configured
`PROJECTFLOW_QUALITY_ATTESTATION_PUBLIC_KEY_FILE` and the receipt
directory, respectively. Never transfer the private key, share HMAC
secrets, or put either artifact in a public repository by default.

After delivery, set the selected task flag in NEC's private staging
EnvironmentFile, restart **only** the Ed25519 staging systemd service,
and verify `QUALITY_ATTESTATION_MISSING`, valid exact-SHA pass,
`QUALITY_ATTESTATION_STALE`, and other-task exemption through the
control plane before moving any further.

The legacy HMAC staging service on port 18765 is separate; it must
not be treated as Ed25519 proof.

## Production gate: not yet enabled

GitHub `main` remains protected. There is no approved PR merge, no
production ProjectFlow deployment and no GitHub-enforced QA branch
ruleset. The ProjectFlow in-app merge gate cannot prevent an external
merge performed directly in GitHub.

A stronger production signer boundary still requires separate OS
principals or a trusted sandbox that excludes all signing material from
agent-controlled tests. The single-user QA demonstration does not
establish that boundary. Promote only after independent review and
production-specific acceptance.
