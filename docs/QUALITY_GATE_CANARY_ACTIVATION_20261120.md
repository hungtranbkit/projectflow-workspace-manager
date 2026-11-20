# Quality Gate canary activation — 20 November 2026

## Actual activation result

**STAGING CANARY ACTIVE; PRODUCTION NOT ENABLED.**

The previously accepted Quality Gate code was deployed on the NEC Linux node
in a dedicated ProjectFlow staging control plane:

- Service: `projectflow-qualitygate-canary.service` (systemd --user).
- Process: `active`, `enabled`, with user linger enabled.
- Listener: **127.0.0.1:18765 only**, no public tunnel or unauthenticated remote exposure.
- Health: `GET /health -> HTTP 200`, `{"status":"ok","auth_mode":"none"}`.
- Tested application source: `60947d94f83706727bb5b085c6a8ed60fc40527a`.
- Database/state: dedicated staging paths; not the production ProjectFlow database.
- Staging repo name: `projectflow-workspace-manager`, repository ID `1`.
- Selected staging task ID `1`: synthetic "Quality Gate Canary Acceptance 2026-11-20" (`LOW` risk).
- Staging task review recorded as PASS and status `READY_FOR_MAIN` strictly for exercising the demo gate; **no real user change or production review was performed**.
- `PROJECTFLOW_QUALITY_PILOT_TASK_IDS=1`, `PROJECTFLOW_QUALITY_PILOT_REPO=projectflow-workspace-manager`.
- Private staging configuration file is mode `0600`; QA receipt is mode `0600`; receipt directory is mode `0700`.
- A staging-only HMAC secret was generated locally; it is **not a production secret**.

## Observed gate outcomes on the running canary's source/config

| Scenario | Observed |
|---|---|
| Selected task 1, receipt deliberately removed | `QUALITY_ATTESTATION_MISSING`, `NO_PR` |
| Selected task 1, valid receipt restored | `NO_PR` (no QA blocker) |
| Selected task 1, different source SHA | `QUALITY_ATTESTATION_STALE` |
| Unselected task 2, same source SHA | Not gated |

The independent-QA signer command ran the pinned real
`tests/test_harness_monitor_report.py` suite and generated an `ATTESTED`
receipt for the exact accepted source SHA. The signer test subprocess uses
a sanitized environment.

**Nothing was pushed, merged or deployed to production**. No real PR was
created and GitHub Actions was not started.

## Provenance and limits

The previous 80-test technical acceptance was at source SHA
`60947d94f83706727bb5b085c6a8ed60fc40527a`.
The staging app runs precisely that SHA, not an untested later branch HEAD.

The canary gate is real within its staging ProjectFlow instance, but the
staging QA signer, staging app and coding agents on NEC may share one OS
account. A mode-0600 secret file **does not separate processes running as
the same user**. This configuration is intentionally **not approved for
production**.

GitHub `main` still does not contain the experiment code; externally
performed GitHub merges cannot be prevented by the staging app. Promotion
needs independently protected QA credentials/principals, GitHub merge
rulesets, and a reviewed deployment of the approved source.

## Operator access and rollback

On the NEC host:
- Local UI: `http://127.0.0.1:18765/tasks/1`
- Service check: `systemctl --user status projectflow-qualitygate-canary.service`
- To stop the canary: `systemctl --user stop projectflow-qualitygate-canary.service`
- To disable autostart: `systemctl --user disable projectflow-qualitygate-canary.service`
- To disable only the gate while keeping staging alive, clear
  `PROJECTFLOW_QUALITY_PILOT_TASK_IDS` in the **private** EnvironmentFile
  and restart the user service.
- Do not paste the HMAC key or private EnvironmentFile into a ticket,
  chat, commit or screenshot.

The receipt expires after 24 hours; absence or expiry blocks the selected
task by design, not a service outage. Re-sign only after an independent
QA run for the current exact SHA.
