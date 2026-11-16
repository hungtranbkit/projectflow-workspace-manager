#!/usr/bin/env bash
# Two disjoint deterministic tiers; neither converts failures to success.
set -euo pipefail
cd "$(dirname "$0")/.."
profile="${1:-full}"
if (( $# )); then shift; fi
fast=(tests/test_unit.py tests/test_smoke.py tests/test_b*.py tests/test_spec_gate.py tests/test_state_invariants.py)
case "$profile" in
  fast) exec ./scripts/test.sh "${fast[@]}" -m 'not live_provider' --strict-markers --timeout=60 --durations=30 "$@" ;;
  remaining)
    excluded=()
    for path in "${fast[@]}"; do excluded+=("--ignore=$path"); done
    exec ./scripts/test.sh tests "${excluded[@]}" -m 'not live_provider' --strict-markers --timeout=60 --durations=30 "$@" ;;
  full) exec ./scripts/test.sh tests -m 'not live_provider' --strict-markers --timeout=60 --durations=30 "$@" ;;
  live)
    # Run only on a disposable, trusted host with a test account, never a
    # shared production node or an untrusted pull-request runner.
    [[ "${PROJECTFLOW_LIVE_TESTS:-}" == 1 ]] || { echo 'Set PROJECTFLOW_LIVE_TESTS=1 for external-provider qualification' >&2; exit 2; }
    command -v claude >/dev/null || { echo 'Missing Claude CLI: live qualification NOT RUN' >&2; exit 2; }
    exec ./scripts/test.sh tests -m live_provider --strict-markers --timeout=600 --durations=30 "$@" ;;
  *) echo 'Usage: scripts/ci-test.sh {fast|remaining|full|live} [pytest arguments]' >&2; exit 2 ;;
esac
