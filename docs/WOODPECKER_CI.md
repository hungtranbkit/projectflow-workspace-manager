# Woodpecker CI migration (staging, NOT cut over)

## Verified local checkpoint (2026-11-21, before any live deployment)

- M910q: checked out exact PR branch in a disposable `/tmp` directory. Both Docker Compose specifications passed `docker compose config --quiet` with example placeholders. `./scripts/preflight.sh` and `./scripts/build.sh` returned exit 0. Woodpecker container not found and no local CI listener was observed; no Docker containers were started or changed.
- MacBook Linux: cloned branch into disposable `/tmp` checkout and ran targeted `pytest` with the existing test environment; **15 targeted tests passed** (Woodpecker API status: 8; readiness guard: 6; ProjectFlow smoke: 1). This is not the entire regression suite.
- Running `python3 infra/woodpecker/readiness.py` with no local secrets returned `BLOCKED`, exit code 2. It does not claim real OAuth, agent registration, or webhook delivery.
- The existing `projectflow.mesflow.net` responded HTTP 200 via an HTTPS request from M910q (certificate verification not tested in that probe); it hosts ProjectFlow and MUST NOT be overwritten. A proposed **separate** `ci.mesflow.net` hostname did not resolve/respond during this probe. Do not assume CI DNS, tunnel, or OAuth callback exists.
- GitHub PR #58 is deliberately a Draft. Do not turn off the existing GitHub Actions workflow or required checks.

### Readiness guard (no runtime changes)

```bash
python3 infra/woodpecker/readiness.py \
  --server-env infra/woodpecker/.env \
  --agent-env infra/woodpecker/.env.agent
```

The output contains only boolean gate results (never client secrets or shared agent secrets). Missing or placeholder configuration returns exit code 2. A green `local_configuration_ready` only confirms local file consistency; it **never** verifies GitHub OAuth, the public HTTPS callback, the private runner connection, webhook delivery, or CI execution. Do not copy the `.env` files onto public/shared nodes to perform this check; run it only on an authorized operator machine holding these secret files, or verify each environment separately.

### External setup still needed

1. Establish a dedicated HTTPS hostname (e.g. `ci.mesflow.net`, **not currently provisioned**), with Cloudflare Tunnel routing only to Woodpecker's loopback HTTP port on a chosen safe server host. Do not hijack the live ProjectFlow hostname or existing tunnels.
2. Register a GitHub **OAuth App**, not a GitHub App, using the new verified Woodpecker hostname as Homepage and `https://<ci-host>/authorize` as callback. Place credentials into an untracked, permission-600 server `.env` on the host.
3. Verify a dedicated, non-production Docker runner and private gRPC connection. Activate only the ProjectFlow repo, run actual PR/push CI, verify status for the exact commit SHA and failure gating; only then plan a separate reviewed GitHub Actions cutover.


Keep GitHub as source/PR forge while moving CI execution to self-hosted Woodpecker. **GitHub Actions stays enabled** until real webhook + pipeline evidence passes.

## Files

- `.woodpecker/verify.yaml`: runs existing preflight/build/full-test/smoke on PR, main push, and manual. No production deployment.
- `infra/woodpecker/compose.yaml`: **server only** (persistent SQLite; HTTP exposed only on loopback; agent gRPC exposed only on a specific private IP).
- `infra/woodpecker/agent.compose.yaml`: dedicated Linux CI runner (Docker socket) **on a separate, isolated machine**.
- `infra/woodpecker/.env.example` and `.env.agent.example`: placeholders only.

## Security / prerequisites

1. Reserve a dedicated Linux Docker runner, no production workloads. A Docker socket mount grants host-level privileges; NEVER run the agent on a shared MESFlow, Zalo Hub, or ESP32 business runtime host.
2. Reserve a real HTTPS hostname for the server. Route hostname through a verified reverse proxy/Cloudflare Tunnel to the server's `127.0.0.1:8179`. Do not publish 8179 or 9000 to the open internet.
3. Create a **GitHub OAuth App** (not GitHub App) at GitHub Developer Settings: Homepage `https://<ci-host>`; callback `https://<ci-host>/authorize`. Set `WOODPECKER_GITHUB_CLIENT` and `WOODPECKER_GITHUB_SECRET` only in local, untracked `.env`.
4. Generate `WOODPECKER_AGENT_SECRET` once with `openssl rand -hex 32`; store securely in server `.env` and isolated runner `.env.agent`. Never print credentials to logs/PR/chat.
5. Keep `WOODPECKER_OPEN=false`; provision the owner account using the documented Woodpecker user CLI before OAuth login. If bootstrap temporarily requires open registration, restrict access to the trusted operator and close registration immediately afterwards.
6. Establish a **private VPN route** (e.g. Tailscale) between runner and server, then bind `CI_GRPC_BIND_IP` to the actual private VPN IP on the server. Firewall TCP 9000 to the dedicated runner's VPN IP only; no public binding. Runner connects to that same private IP on port 9000.

## Server bootstrap (after verifying credentials and hostname)

```bash
cd infra/woodpecker
cp .env.example .env && chmod 600 .env
# Edit .env locally; do not commit
docker compose --env-file .env -f compose.yaml config --quiet
docker compose --env-file .env -f compose.yaml up -d
docker compose --env-file .env -f compose.yaml ps
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8179/
```

## Dedicated runner bootstrap (different host, only after private network established)

```bash
cd infra/woodpecker
cp .env.agent.example .env.agent && chmod 600 .env.agent
# Set actual private WOODPECKER_SERVER address and matching AGENT_SECRET
docker compose --env-file .env.agent -f agent.compose.yaml config --quiet
docker compose --env-file .env.agent -f agent.compose.yaml up -d
docker compose --env-file .env.agent -f agent.compose.yaml ps
```

OAuth login → activate ONLY `hungtranbkit/projectflow-workspace-manager` → confirm webhook delivery and agent registration in the Woodpecker UI.

## Acceptance before retiring GitHub Actions

- PR webhook reaches Woodpecker; run references matching GitHub repository and commit SHA.
- Real preflight, build, full test, smoke execute with timestamped logs and clear results; failure blocks success.
- A main push reports the correct GitHub commit status; no stale result considered passing.
- Queue, retry, runner-disconnect and fork-PR approval behave safely.
- No secrets passed into PR pipelines; no deployment credentials or production Docker sockets exposed.
- Only after proof and required GitHub status checks are migrated, disable `.github/workflows/ci.yml` through a **separate reviewed change**.

## ProjectFlow monitoring

No verified Woodpecker status API wiring yet: show `UNKNOWN/NOT_CONNECTED`, never PASS. Later map repo + exact SHA + run ID + timestamps to read-only ProjectFlow CI state, separately from QA/merge/deployment. CI PASS does not mean DEPLOYED or business DONE.

Official documentation:
- https://woodpecker-ci.org/docs/administration/installation/docker-compose
- https://woodpecker-ci.org/docs/administration/configuration/forges/github
- https://woodpecker-ci.org/docs/usage/security-boundaries
