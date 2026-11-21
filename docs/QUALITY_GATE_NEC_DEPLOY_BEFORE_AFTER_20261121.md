# ProjectFlow Quality Gate — NEC deployment & before/after evaluation (21 November 2026)

## Executive result

**DEPLOYED to the running NEC Quality Gate canary; NOT deployed to ProjectFlow production or GitHub main.**

The existing staging user service `projectflow-qualitygate-ed-stage.service`
was upgraded from code commit `e33d475613b1e8f3480d885f4fc86e1efc8e7d5e`
to `e121016a24aa0620b256653b79f32e8081e07a54` with a database/config
snapshot and tested rollback path. Source was fetched from the existing
`integrate/quality-gate-main-20261121` GitHub branch. No GitHub Actions
was launched, no PR was created, and `main` remained unchanged.

## Deployment evidence and rollback

- Host: NEC Linux `rag-PC-VKL24XZG3`.
- Service: `projectflow-qualitygate-ed-stage.service` — **active**, **enabled**.
- Bind: `127.0.0.1:18766` only. No public access exposed.
- `GET /health` -> `{"status":"ok","auth_mode":"none"}`.
- Canary task `#1`: `READY_FOR_MAIN`, `ready_for_main=true`.
  This is a synthetic staging task, not a production code change.
- Source actually loaded by the service: app checkout at
  `.../qualitygate-ed-staging-20261121/green-20261121/app-src`,
  commit `e121016a24aa0620b256653b79f32e8081e07a54`.
- Original staging database was preserved; post-upgrade SQLite
  `PRAGMA integrity_check=ok`, and the task count remained `1`.
- Before upgrade, a private backup was created at
  `/home/rag/workspace/qualitygate-ed-staging-20261121/rollback-20261121`.
  It contains a consistent SQLite backup (integrity check OK) and
  the prior user-service unit and private environment settings.
- A separate **green** user-service on `127.0.0.1:18768` was started
  against a copied database and source repo. Health, task status and
  signed-gate decision were validated before changing the main canary.
- After swapping only the real canary unit's `WorkingDirectory` and
  restarting it, health and task decision passed. The green test service
  was stopped and its source/data checkout retained for recovery.
- Rollback if necessary: restore the old service unit from
  `rollback-20261121/unit-pre-upgrade.service`, then
  `systemctl --user daemon-reload` and
  `systemctl --user restart projectflow-qualitygate-ed-stage.service`.
  Assess any intervening DB writes before restoring the old SQLite
  snapshot; do NOT overwrite a live DB blindly.

## Before/after measurements (same NEC host)

| Measurement | Before (e33d475) | After (e121016) | Interpretation |
| --- | --- | --- | --- |
| `/health` median local HTTP latency (10 sequential samples) | **2.02 ms** | **1.83 ms** | 0.19 ms lower, ~9% on this tiny sample; **not statistical proof of a real performance gain** |
| `/health` min–max latency | 1.73–3.81 ms | 1.77–2.15 ms | Variability exists; load not controlled |
| Health status | OK | OK | No observed health regression |
| Canary task status | READY_FOR_MAIN | READY_FOR_MAIN | Preserved |
| Staging SQLite integrity | OK | OK | No detected DB corruption |
| Selected-task Ed25519 receipt check | Supported | Supported; valid receipt accepted | No loss of signature functionality |
| Missing receipt | Rejected | `QUALITY_ATTESTATION_MISSING` | Fail closed |
| Wrong source SHA | Rejected | `QUALITY_ATTESTATION_STALE` | Exact-commit protection preserved |
| Isolated Docker QA runtime | Not available in the old staging source | Available in the new source; explicitly tested | Concrete added capability, subject to opt-in |
| Mandatory container-signed proof | Not supported | Supported, **currently disabled** | Cannot claim enforced independence yet |

All local timing measurements are from ten `curl` requests to the
loopback health endpoint; they do not measure merge latency, real-world
user traffic, or end-to-end QA duration.

## Concrete improvements delivered

1. **Scoped merge control:** ProjectFlow's application can require
   independently signed Ed25519 proof tied to task ID, repo ID/name,
   source Git commit, fixed QA policy and time of issue. The legacy
   merge bypass and direct local integration are checked by the same
   opt-in gate. The current main branch does not contain this quality
   gate; this improvement is active **only** on the selected staging
   canary task.
2. **Tamper resistance:** altered receipts, missing proof and other
   commits do not satisfy the signed QA gate; other unselected tasks
   remain unaffected. The receipt expires after 24 hours.
3. **Real isolation capability:** a pinned QA runner can execute
   `tests/test_harness_monitor_report.py` inside an unprivileged,
   no-network, read-only Docker container with bounded memory/CPU/PIDs
   and no source `.git` or Docker socket mount. The required temporary
   SQLite directories were moved into bounded container `/tmp`.
4. **Evidence-based QA:** the new code underwent **90/90 passing**
   targeted tests, including a real Docker runtime pytest acceptance,
   on exact QA commit
   `2d23d8eafedbbeefe5c72b8b0445520819243ca6`.
   Later updates to the branch before this deployment were only
   the acceptance report; executable Quality Gate code was unchanged.
5. **Recoverable release:** a green service with separate cloned DB
   and a rollback snapshot was used; the old service remains recoverable
   without changing production.

## Remaining blockers — do not overstate readiness

- **This is a staging deployment, not production**. The real Dell Linux
  ProjectFlow host was not available through Terminal MCP. A Tailscale
  ping to Dell timed out and SSH authorization was denied. No active
  production ProjectFlow Workspace Manager web service was confirmed
  on the checked online hosts.
- `PROJECTFLOW_QUALITY_REQUIRE_ISOLATED_QA=1` is currently **OFF**
  in the live NEC staging environment: its existing receipt was signed
  during a host-direct canary, **not independently under the new
  Docker-isolation requirement**.
- Production deployment needs a separately controlled QA signer
  principal, an authenticated transfer of the public verification key
  and signed receipt, then real staging acceptance in strict mode.
  Prior attempts to send MacBook-signed artifacts through the
  connected command channel were blocked; no transfer was claimed.
- The current fixed policy tests only the ProjectFlow
  monitor-report pilot, **not all ProjectFlow repositories/tasks**.
  Other tasks require reviewed scope-specific test policies.
- Direct GitHub merges outside ProjectFlow can bypass the application
  check unless GitHub branch protection/rulesets are configured
  appropriately. The current `.github/workflows/ci.yml` still triggers
  GitHub Actions on Pull Requests and pushes to main. None was started
  in this operation.

**Decision:** stage upgrade accepted; production readiness still gated by
access to the actual host, independent QA signer enforcement, GitHub
merge protection and reviewed task policy expansion.
