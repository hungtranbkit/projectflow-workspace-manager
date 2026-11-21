#!/usr/bin/env bash
# Operator-triggered offline smoke of Woodpecker's REAL CLI local backend.
# Runs only trusted scripts in this checkout; no webhook, no live CI server,
# no Docker, no fork PR content, and no production deployment.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cli="${WOODPECKER_CLI_BIN:-$HOME/.local/share/projectflow-woodpecker-staging/v3.20.1/woodpecker-cli}"
[[ -x "$cli" ]] || { echo 'BLOCKED: verified woodpecker-cli binary required' >&2; exit 2; }
[[ -f "$repo/PROJECT.yaml" && -x "$repo/scripts/preflight.sh" ]] || { echo 'BLOCKED: project contract missing' >&2; exit 2; }
tmp="$(mktemp -d "${TMPDIR:-/tmp}/pf-wp-cli-gates.XXXXXXXX")"
chmod 700 "$tmp"
trap 'rm -rf -- "$tmp"' EXIT
cat >"$tmp/positive.yaml" <<'YAML'
when:
  - event: manual
steps:
  - name: preflight-build
    image: bash
    commands:
      - ./scripts/preflight.sh
      - ./scripts/build.sh
YAML
cat >"$tmp/negative.yaml" <<'YAML'
when:
  - event: manual
steps:
  - name: deliberate-failure
    image: bash
    commands:
      - exit 17
  - name: must-not-run
    image: bash
    commands:
      - echo SECOND_STEP_SHOULD_NOT_RUN
YAML
"$cli" lint --strict "$repo/.woodpecker/verify.yaml" >"$tmp/lint-main.log" 2>&1
"$cli" lint --strict "$tmp/positive.yaml" >"$tmp/lint-positive.log" 2>&1
"$cli" lint --strict "$tmp/negative.yaml" >"$tmp/lint-negative.log" 2>&1
echo 'PASS: all three real Woodpecker CLI strict-lint checks'
"$cli" exec --backend-engine local --pipeline-event manual --repo-path "$repo" "$tmp/positive.yaml" >"$tmp/positive.log" 2>&1 || {
  echo 'FAIL: CLI local preflight/build pipeline' >&2
  exit 1
}
grep -q 'preflight PASS' "$tmp/positive.log"
grep -q 'build PASS' "$tmp/positive.log"
echo 'PASS: Woodpecker CLI ran preflight and build'
if "$cli" exec --backend-engine local --pipeline-event manual --repo-path "$repo" "$tmp/negative.yaml" >"$tmp/negative.log" 2>&1; then
  echo 'FAIL: Woodpecker accepted deliberately failing command' >&2
  exit 1
fi
grep -q 'exit code 17' "$tmp/negative.log" || {
  echo 'FAIL: negative test failed for a reason other than exit 17' >&2; exit 1
}
if grep -q '\[must-not-run:' "$tmp/negative.log"; then
  echo 'FAIL: step after a hard failure ran' >&2; exit 1
fi
echo 'PASS: Woodpecker correctly blocked the next step after exit 17'
echo 'NOT_VERIFIED: GitHub webhook, OAuth, dedicated Docker runner, full regression, GitHub commit status'
