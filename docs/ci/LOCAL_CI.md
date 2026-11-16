# ProjectFlow local CI

GitHub Actions is retired for this repository. GitHub stores source, PRs and
commit statuses; Dell executes tests without an Actions runner/registration.
This is CI infrastructure only (NO_BEHAVIOR_CHANGE to application gates).

## Execution and merge gate

`projectflow-local-ci.timer` checks open PRs every two minutes after the previous
worker exits. Only non-draft PRs authored by `hungtranbkit`, from this exact repo,
targeting main are eligible. Forks/other authors require explicit review and an
operator-controlled change to the allowlist; they never receive a guessed PASS.

One flock-protected worker runs at a time. Each job records head + base SHA,
merges that exact base into an ephemeral checkout, and tests the resulting tree.
A change to either SHA cancels the running job. Result context: `local-ci/verify`.
Require this context on main, strict/up-to-date, enforce admins, and PRs. Do not
merge using admin bypass. The workflow file is removed from .github/workflows;
`retired-github-actions.yml` is documentation, not an executable workflow.

The host creates a dedicated privileged Docker-in-Docker container for each
TRUSTED first-party PR. No host Docker socket, home, gh configuration or API token
is mounted into it. It has 2 CPUs, 4 GiB and a 2048 PID limit; no published ports.
All test `wm-*` containers belong to its inner daemon; host cleanup removes only
the unique `pf-ci-*` container after checking its ownership label. This is NOT a
sandbox for hostile PRs: privileged DinD requires the stated trust boundary.

The outer image installs Python in its own venv. The guest preloads nginx and
python sandbox images, installs the test dependencies, runs preflight, then the
fast and remaining deterministic tiers. Real security/Docker tests are retained.
Per-test timeout: 60s; overall active job: 60 minutes. Failed exit, missing/empty
JUnit, failure/error in JUnit, skipped security/isolation cases, stale SHA or
cleanup failure prevents SUCCESS. Nine real Claude-provider tests remain separate
qualification on an isolated trusted machine/account; do not call deterministic
CI evidence a live-provider PASS. No credentials for live providers enter CI.

Failed revisions are recorded once, not retried forever. A new commit/base triggers
a new run. Transient failure retry: inspect result/log, stop the service, remove
only that exact run's result.json, restart the service. Do not erase evidence logs.
Completed status is published once; publication failure retries next tick.
Crash recovery cleans the exact recorded owned container before retrying.

## Install and operate

Run `scripts/local-ci/install.sh` on Dell as its normal user. It installs the
controller outside the checkout, builds `projectflow-local-ci:1`, and installs
user systemd service/timer. Ensure user lingering is enabled for startup without
interactive login. The timer is local, not a ChatGPT recurring automation.

- Status: `systemctl --user status projectflow-local-ci.timer projectflow-local-ci.service`
- Logs: `journalctl --user -u projectflow-local-ci.service`
- Evidence: `~/.local/share/projectflow-local-ci/state/runs/pr-<number>-<head>-<base>/`
- Each run: result.json, run.log, fast.xml, remaining.xml, environment.txt, packages.txt.
- Stop: `systemctl --user disable --now projectflow-local-ci.timer`; stop the service
  and inspect its recorded owned container if cancelling an active job.
- Rollback is explicit: restore a reviewed CI design and required checks before
  changing the merge gate. Do not enable Actions or paid usage automatically.

No public log endpoint is exposed. GitHub commit status shows the result; operators
can inspect local evidence through existing authenticated machine access.

## Validation

Controller contract tests cover first-party trust, base drift, missing/failing
JUnit, skipped security tests, ownership cleanup, no repeat execution/status spam,
nonzero process exit, and publication only for the exact tested revision.
The fixture repair carried with this migration changed a controlled 226-case
sample from 58 pass/148 fail/20 errors to 226 pass. Remaining full Docker-backed
qualification must complete on Dell before merging this PR. Do not substitute
those unit tests or a partial Docker-free local run for the full merge gate.
