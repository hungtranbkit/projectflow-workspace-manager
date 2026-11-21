# Woodpecker CI migration (staging, not cut over)

Goal: keep GitHub as source/PR forge while moving CI execution to self-hosted Woodpecker. The current GitHub Actions workflow remains in place until real-world acceptance.

## What is versioned

- `.woodpecker/verify.yaml`: on PR, push to `main`, or manual run; installs test dependencies then runs actual `preflight`, `build`, complete `test`, and `smoke` commands, failing closed on any nonzero exit.
- `infra/woodpecker/compose.yaml`: Woodpecker server with persistent SQLite volume, GitHub OAuth, loopback-only UI port; agent is an explicit profile, disabled until a dedicated CI machine is confirmed.
- `infra/woodpecker/.env.example`: placeholders only (no secrets).

## Requirements and security boundary

1. Provision a dedicated Linux Docker runner **without production workloads**. A Woodpecker Docker agent mounting `/var/run/docker.sock` has host-equivalent power. Do **not** enable the `dedicated-runner` profile on an HP/M910 machine hosting business-critical containers.
2. Obtain a real HTTPS hostname and route it to the server host's `127.0.0.1:8179` (for example, through a correctly scoped Cloudflare Tunnel). No public router port forwarding and no exposing port 9000.
3. Create a **GitHub OAuth App** (not GitHub App) with Homepage `https://<ci-host>` and Authorization callback `https://<ci-host>/authorize`. Woodpecker's GitHub forge driver needs OAuth client ID and client secret; these are an external bootstrap prerequisite.
4. On the server host, create `infra/woodpecker/.env` from the example, set `WOODPECKER_HOST` to the verified HTTPS URL, and generate an independent random `WOODPECKER_AGENT_SECRET` using `openssl rand -hex 32`. Use file mode `600`. Do not copy credentials to chats/logs/commits.
5. Registration remains closed by default (`WOODPECKER_OPEN=false`). Provision only the admin account using the official Woodpecker user CLI before first sign-in. If temporary open registration is ever used during a tightly controlled bootstrap, close it again immediately after the owner logs in; do not expose open registration on the public internet.

## Server bootstrap (only after prerequisites)

```bash
cd infra/woodpecker
cp .env.example .env
chmod 600 .env
# Edit .env locally; use actual secret values and HTTPS hostname.
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d woodpecker-server
docker compose --env-file .env ps
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8179/
```

Once the server and OAuth login are working, activate ONLY `hungtranbkit/projectflow-workspace-manager` in Woodpecker. Enable a dedicated runner on the isolated CI host using the same agent secret and a verified, restricted network connection to the Woodpecker server. The Compose `dedicated-runner` profile is only for that dedicated host, not automatically part of server bootstrap.

## Acceptance checklist (do not claim complete until evidenced)

- An actual GitHub PR webhook reaches Woodpecker and creates a single run.
- Logs have timestamps, correct commit SHA, and clear PASS/FAIL for preflight/build/test/smoke; a failing test blocks success.
- A push to main is recorded, and Woodpecker reports commit status to GitHub.
- A failed/queued run is visible and retry works; agent disconnect does not cause fake success.
- Confirm at least one real full-suite run; failures are reported, not hidden.
- Confirm the pipeline never has deploy tokens and cannot modify production.
- Only after real acceptance and required GitHub checks migration, disable `.github/workflows/ci.yml` in a **separate** reviewed change.

## ProjectFlow visibility

Keep the existing ProjectFlow monitoring read-only. Until verified Woodpecker status API integration exists, label Woodpecker CI runs as `UNKNOWN/NOT_CONNECTED`, not PASS. In follow-up, correlate GitHub repo + commit SHA + Woodpecker pipeline ID and timestamp; report `QUEUED`, `RUNNING`, `PASS`, `FAIL` distinctly from actual deployment. Do not let a green CI run alone mark a ProjectFlow task DONE or deployed.

Official references:
- https://woodpecker-ci.org/docs/administration/installation/docker-compose
- https://woodpecker-ci.org/docs/administration/configuration/forges/github
- https://woodpecker-ci.org/docs/usage/security-boundaries
