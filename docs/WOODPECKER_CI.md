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

## Native Linux staging verified — 2026-11-21

**Actual result:** Official Woodpecker v3.20.1 linux/amd64 binary downloaded on MacBook Linux from the upstream release and SHA-256 verified against its published checksums. `woodpecker-server --version` returned 3.20.1. `bash scripts/woodpecker_native_smoke.sh` started the real server on loopback `127.0.0.1:8187` and received HTTP **200**, then killed the test process and discarded its temporary SQLite data. This is **not** an OAuth, GitHub webhook, runner or CI pipeline PASS.

MacBook Linux had approximately 373 GB free and 12 GB available RAM, but no Docker/Podman. HP had Docker but its root filesystem was ~96% full and served active workloads; M910q's Docker daemon was unavailable with root filesystem ~88% full. **Do not install a Docker runner on HP or M910q as currently configured.** The native Local backend does not isolate untrusted PR code and is not a safe substitute for a dedicated runner.

A disabled, unloaded-from-autostart **user systemd service** was installed successfully on MacBook Linux using `bash infra/woodpecker/native/install-user-service.sh`. It is `disabled/inactive`, with files:
- `~/.config/systemd/user/projectflow-woodpecker.service`
- `~/.local/share/projectflow-woodpecker-staging/native/run-server.sh`
- `~/.config/projectflow-woodpecker/server.env.example`

No real `server.env` was created; no active service was left behind. The runner/server secrets must be stored on authorized hosts, not in this repository. The entrypoint fails closed without an HTTPS hostname, real GitHub OAuth App credentials, 64-hex agent/GRPC secrets and closed registration. Server HTTP and gRPC bind only to loopback, so a future remote runner also requires private-network routing.

Remaining external requirements: provision a **separate** HTTPS hostname and Cloudflare Tunnel for CI (do not overwrite the existing ProjectFlow domain), create a GitHub **OAuth App** with callback `https://<ci-host>/authorize`, and choose a safe dedicated Docker runner. Cloudflared tunnel administration on M910q was unavailable; HP had no `cert.pem`, so no DNS changes were attempted. Keep GitHub Actions and PR #58 Draft until a real SHA-matched PR/push pipeline, negative test, queue retry and GitHub commit status are verified.


## Additional independent Woodpecker verification (21 November 2026, 13:32–13:45 ICT)

This section records **commands actually executed** and separates results from remaining prerequisites.

- Downloaded the upstream official `woodpecker-cli_linux_amd64.tar.gz` and `woodpecker-agent_linux_amd64.tar.gz`, verified **both SHA-256 hashes against the 3.20.1 upstream release checksums**, and observed CLI/agent versions `3.20.1`.
- Validated `.woodpecker/verify.yaml` with **`woodpecker-cli lint --strict`**, success exit code 0, not merely YAML parsing.
- Ran `bash scripts/woodpecker_agent_smoke.sh` on MacBook Linux against an **ephemeral server and agent using the Local backend**, loopback-only ports 8191/9191, randomly generated throwaway credentials, no public OAuth and no registered GitHub repository. Observed **SERVER_HTTP_PASS**, **AGENT_GRPC_REGISTRATION_PASS**, and script exit code 0. Both processes and the temporary SQLite DB were removed. This proves a genuine server/agent gRPC handshake, **not** actual pipeline queue execution by the server.
- Ran `woodpecker-cli exec --backend-engine local` against a controlled temporary fixture in the disposable ProjectFlow checkout: `./scripts/preflight.sh` PASS, `./scripts/build.sh` PASS, CLI exit 0. A separate negative fixture deliberately failed with `exit 17`; Woodpecker returned failure and did **not** run the next step. Source-controlled reproducible wrapper: `bash scripts/woodpecker_cli_gates_smoke.sh`, exit 0.
- Previewed merging current `main` (`27164f7`) into the PR checkout using `git merge-tree --write-tree`, **clean**. Created a local non-fast-forward merge and reran strict lint, positive/negative CLI smoke and the 15 targeted pytest tests: all PASS. **The local merge was NOT pushed**: MacBook Git transport does not have working write credentials (`fatal: could not read Username for 'https://github.com'`). The GitHub PR branch is unchanged by that local merge; do not claim the PR is rebased/up-to-date.
- Runner selection: MacBook had no Docker/Podman/VM tools, no passwordless sudo, and `unshare -Ur` failed; `newuidmap`, `newgidmap`, `rootlesskit`, `slirp4netns` and `fuse-overlayfs` were absent. NEC had Docker but six running containers and only about 12 GB available root disk. HP and M910 remain unsuitable for a privileged Docker socket runner due to existing workloads/space.
- **SECURITY**: the Local backend executes pipeline code with the user account's privileges and can read local agent secrets. Do **not** connect the disposable Local smoke setup, or its script, to public pull requests. It is intentionally ephemeral.
- Remaining blocker remains **real GitHub OAuth App + dedicated HTTPS hostname/DNS/tunnel administration + isolated Docker runner**. None was created or enabled. Keep existing GitHub Actions and PR #58 Draft until actual SHA-matched public webhook, fail-closed full test run, status reporting and retry evidence.


### Local server credentials staging (2026-11-21)

MacBook now has `/home/mac/.config/projectflow-woodpecker/server.env`, created by
`python3 infra/woodpecker/native/prepare-secrets.py`, with mode **0600**.
The file contains two independent randomly generated 64-character hex secrets
(`WOODPECKER_AGENT_SECRET`, `WOODPECKER_GRPC_SECRET`), OAuth placeholders,
`WOODPECKER_GITHUB=true`, `WOODPECKER_GITHUB_PUBLIC_ONLY=true`, and
`WOODPECKER_OPEN=false`. Existing files are never overwritten.
**Neither secret value is printed nor committed.**

The guarded server entrypoint, when invoked with that environment, returns exit 2
and reports only three outstanding fields: `HTTPS_HOST`, `GITHUB_CLIENT`,
`GITHUB_SECRET`. It remains `disabled/inactive`; no external service was
started. Before activation, use a unique real HTTPS hostname and real owner-created
GitHub OAuth App, with callback `https://<hostname>/authorize`; protect the
configured secret file and ensure the dedicated isolated Docker runner is ready.

Do not mistake having locally generated secrets for successful OAuth registration.
Local server/agent handshake and Woodpecker CLI tests are PASSED, while public
webhooks, full-suite CI, GitHub status checks and safe dedicated runner remain
**UNVERIFIED**.


## Interim automatic trusted-main CI (activated on MacBook, 2026-11-21)

This is an **interim fallback** while the real GitHub OAuth App, separate HTTPS
hostname, and isolated Docker runner for the normal Woodpecker Server remain
unavailable. It does not replace required PR checks.

The versioned components are:
- `scripts/woodpecker_main_poll.py`: SHA-pinned public-GitHub `main` poller,
  no writable GitHub token or webhook, single-process lock, bounded job time,
  retry cooldown, explicit PASS/FAIL state and timestamped log evidence.
- `infra/woodpecker/local-main/trusted-main.yaml`: preflight/build/full-test/smoke
  via the official `woodpecker-cli exec --backend-engine local` engine.
- `infra/woodpecker/local-main/install-user-timer.sh`: systemd service and timer
  installer; the timer can be enabled with the explicit `--enable` flag.
- `tests/test_woodpecker_main_poll.py`: 9 offline poller behavior tests.

**Actual MacBook installation:** versioned poller and trusted-main YAML copied to
`~/.local/share/projectflow-woodpecker-main-ci/`, user service/timer units
created and verified by `systemd-analyze --user verify`. The timer
`projectflow-woodpecker-main-ci.timer` was **enabled and active**;
`loginctl show-user mac -p Linger` returned `Linger=yes`. A live first run
was dispatched at approximately 17:35 Asia/Bangkok on commit prefix
`072b22bd5ab7`; the state JSON showed RUNNING and the log demonstrated
real Woodpecker CLI strict lint, preflight PASS, build PASS and entry into
the complete test suite. **Do not infer the final test outcome from this
checkpoint**; inspect the latest status file.

To inspect the real evidence:
```bash
systemctl --user status projectflow-woodpecker-main-ci.timer
systemctl --user status projectflow-woodpecker-main-ci.service
cat ~/.local/state/projectflow-woodpecker-main-ci/status.json
journalctl --user -u projectflow-woodpecker-main-ci.service -n 80 --no-pager
```
Full logs are written as mode-0600 files under
`~/.local/state/projectflow-woodpecker-main-ci/logs/`. The poller only builds
the exact Git SHA currently resolved from the **protected main branch**. It
does not receive or execute code from PR branches or forks, and reports a
failed test as FAIL rather than faking a green result.

**Security boundary:** Woodpecker's Local backend has no OS/container isolation.
Even with scrubbed environment variables and resource limits, code executed by
this job shares the host user's filesystem permissions. Treat this as an
interim trusted-main-only CI worker, not a safe public PR runner. Never add
personal tokens, deploy keys, fork PRs or production pipelines to it. The
systemd service is capped at 125% CPU, 4 GB memory and a bounded runtime.

This automatic trusted-main mode **does not produce GitHub commit statuses**,
does not evaluate unmerged PRs, does not connect to the Woodpecker web UI,
and does not justify disabling the existing required GitHub Actions checks.
The full Woodpecker OAuth/tunnel/isolated-runner conversion remains a separate
acceptance gate.


## Live automatic protected-main CI checkpoint — 2026-11-21 (17:35 ICT)

This is a **real, enabled** user systemd timer on MacBook Linux:
`projectflow-woodpecker-main-ci.timer`, scheduled every 15 minutes with
`Linger=yes`. Its worker runs the official `woodpecker-cli exec` Local
backend against the current protected `main` SHA. It writes a mode-0600
JSON state file and logs at:
`~/.local/state/projectflow-woodpecker-main-ci/status.json`
and `logs/<sha-prefix>-attempt<N>.log`. It uses a non-blocking file
lock, pins the exact SHA, caps resource consumption to 125% CPU / 4GiB RAM
and never executes PR/fork code.

The first observed SHA was `072b22bd5ab7`. The first full test attempt
**FAILED truthfully**, because `tests/test_repository_runtime_sandbox.py`
invokes `docker info` at module collection time while MacBook lacked the
Docker CLI, raising `FileNotFoundError: docker`. This is not a passing test
run. The official Docker **CLI-only** v29.10.0 (11MB) was downloaded from
`download.docker.com`, SHA-256 checked against its official sidecar file,
and installed privately into the CI venv without sudo/daemon. It now
responds to `docker --version`; `docker info` correctly exits 1 because
there is no Docker daemon. Tests with explicit daemon-unavailable skip
conditions can now collect. Repeatable installer:
`bash infra/woodpecker/local-main/install-docker-cli.sh`.

The worker has a bounded one-time operator retry path
`python3 woodpecker_main_poll.py --retry-now`, still respecting the
three-attempt limit and the locking guard. Ten unit tests verify the poller,
including this guard. A second run of the first SHA was dispatched under a
resource-limited systemd transient service, entering the full test gate.
**Only the live `status.json` and final log can attest its eventual
PASS/FAIL**. No claim of a full test PASS is made here.

This is a *trusted-main-only interim solution*, **not** completed replacement
of GitHub Actions PR checks, production isolation, the Woodpecker dashboard,
GitHub OAuth or a public webhook receiver. GitHub Actions remains unchanged.
Stop the interim scheduler if a trusted-main/local-backend security policy
cannot be maintained:
`systemctl --user disable --now projectflow-woodpecker-main-ci.timer`.

