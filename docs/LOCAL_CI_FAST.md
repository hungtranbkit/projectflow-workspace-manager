# ProjectFlow Fast CI — native Woodpecker CLI (interim)

**Status (2026-11-21): verified and running on the MacBook Linux node.**
This is NOT the same as full Woodpecker Server CI or GitHub Actions PR checks.

## Goal and strict boundary

- Keep GitHub as the source of truth; poll only the repository's protected `main` ref every 15 minutes.
- Pin the exact Git commit SHA before running commands. Execute nothing from unreviewed branches, forks, issue descriptions, or webhook payloads.
- Use **Woodpecker CLI 3.20.1** in its **Local backend** to run the preflight, build, app smoke, and established harness fast tier.
- Do not expose a CI HTTP listener, use deploy secrets, or change production services.
- Worker is a **trusted-main-only interim fallback** and cannot give GitHub a PR required-status check. Woodpecker Local does not isolate code: the tests run with the MacBook user account's filesystem privileges. Never extend it to public/unreviewed PRs. An isolated Docker runner is still required for full CI.

## Real acceptance evidence from 2026-11-21

| Gate | Result |
| --- | --- |
| Protected-main SHA detection and clone | PASS (`072b22bd5ab7...`) |
| Official Woodpecker CLI strict lint | PASS |
| `./scripts/preflight.sh` | PASS |
| `./scripts/build.sh` | PASS |
| `./scripts/smoke.sh` | PASS (1/1) |
| `./scripts/ci-test.sh fast` | PASS (33/33) |
| Actual systemd worker final state | `FAST_PASS`; around 42–43 seconds |
| Duplicate same-SHA detection | PASS (`ALREADY_PASSED`, does not rebuild) |
| Worker unit tests | 18/18 PASS |
| Full regression test | NOT PASS; requires a Docker daemon/isolated runner |
| GitHub PR status checks | NOT CONNECTED |

The **previous attempt** used Python 3.14, allowed pytest to continue long after failures, and eventually produced `BlockingIOError: Resource temporarily unavailable`, thousands of lines of cascading failures, and a ~32-minute failed run. Follow-up diagnosis under the correct Python 3.11 with `--maxfail=1` identified an application sandbox test that expects Docker-backed `RUNNING` state. The MacBook has Docker **CLI only** and no daemon. The new Fast CI does not silently skip a failing full suite: it explicitly reports `full_regression: NOT_RUN_NO_ISOLATED_DOCKER_RUNNER`.

## Components

- `scripts/woodpecker_main_poll.py`: exact SHA polling, single-worker file lock, fail-closed status JSON, attempt limit 3 per SHA/profile, 1-hour retry cooldown, stale-run recovery, timestamped last poll, immutable histories, unique mode-0600 logs, bounded streaming output, command timeout.
- `infra/woodpecker/local-main/trusted-main.yaml`: Fast CI definition; **not** the full container workflow.
- `infra/woodpecker/local-main/install-python311.sh`: installs managed Python **3.11.18** under the CI application home using `uv`; no sudo/system Python changes.
- `infra/woodpecker/local-main/install-docker-cli.sh`: installs Docker **CLI 29.10.0 only** with verified static release checksum; does not install/start a Docker daemon.
- `infra/woodpecker/local-main/install-user-timer.sh`: managed user service/timer; default safe disabled; supports `--update` for its own known units and `--enable` to opt in.
- `tests/test_woodpecker_main_poll.py`: policy, retries, log bounds, crash recovery, runtime, record integrity and secret-scrubbing tests.

## Install on a dedicated, non-production Linux user

First obtain Woodpecker CLI v3.20.1 from its official release and **verify its checksum** against `checksums.txt` (do not trust an unverified binary). This package expects the verified binary at:
`~/.local/share/projectflow-woodpecker-staging/v3.20.1/woodpecker-cli`.

Then:
```bash
bash infra/woodpecker/local-main/install-python311.sh
bash infra/woodpecker/local-main/install-docker-cli.sh
bash infra/woodpecker/local-main/install-user-timer.sh   # installs but does NOT enable
# Review trust boundary and machine limits, then explicitly opt in:
bash infra/woodpecker/local-main/install-user-timer.sh --update --enable
systemctl --user start projectflow-woodpecker-main-ci.service  # manual first test
```

Install refuses to overwrite pre-existing units unless `--update` is specified and checks that they are known ProjectFlow CI units. The service is resource-limited (125% CPU, 4 GiB RAM, 200 tasks, 20-minute systemd timeout; pipeline subprocess has a 15-minute timeout), while log output is capped at ~2.5 MB per run. Test runtime is pinned to Python 3.11.18 and `PYTEST_ADDOPTS` includes fail-fast and short tracebacks.

## Observe and manage

```bash
systemctl --user is-active projectflow-woodpecker-main-ci.timer
systemctl --user list-timers projectflow-woodpecker-main-ci.timer
systemctl --user status projectflow-woodpecker-main-ci.service
cat ~/.local/state/projectflow-woodpecker-main-ci/status.json
journalctl --user -u projectflow-woodpecker-main-ci.service -n 60 --no-pager

# Stop just this CI timer without touching production services:
systemctl --user disable --now projectflow-woodpecker-main-ci.timer
```

Expected state fields include `sha`, `status` (`RUNNING`, `FAST_PASS`, `FAIL`, `INTERRUPTED`), `gate_level: FAST`, `full_regression`, `github_pr_checks`, `profile`, `last_polled_at`, `log_path`, `attempts` and `elapsed_seconds`. Repeated SHA is skipped after `FAST_PASS` but updates the last poll timestamp. A change to trusted pipeline content changes its SHA-256-based policy identity, requiring a fresh result on the same git commit. A network outage preserves the last verified result but records the failure to poll.

## Remaining work before replacing GitHub Actions completely

The current `.github/workflows/ci.yml` and GitHub branch protection remain unchanged. To replace GitHub-hosted PR CI safely, provision a separate HTTPS hostname, a real **GitHub OAuth App** for Woodpecker Server, and an **isolated Docker runner** with sufficient capacity, then verify real PR webhook delivery, a full regression run, negative failure gating, queue/retry behavior, and a required GitHub check posted on the exact SHA. Do not equate this locally passing Fast tier with the full test suite or with deployment.
