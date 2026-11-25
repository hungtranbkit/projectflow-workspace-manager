#!/usr/bin/env bash
set -euo pipefail
# Required local-ci/verify contract. Both modes run substantive tests and
# produce the exact JUnit artifacts expected by the protected branch checker.
# Scoped to the Harness/monitoring surface; not a claim of full ProjectFlow QA.
if [[ $# -lt 1 ]]; then
  echo "usage: scripts/ci-test.sh fast|remaining [pytest args...]" >&2
  exit 2
fi
tier="$1"; shift
case "$tier" in
  fast)
    set -- tests/test_fleet_steward.py \
      tests/test_cc95_map.py \
      tests/test_sidebar_nav.py \
      tests/test_harness_progress.py \
      tests/test_harness_monitor_report.py \
      tests/test_harness_trajectory.py \
      tests/test_harness_monitor_service.py \
      "$@"
    ;;
  remaining)
    set -- tests/test_harness_zgh_node_java_qa.py \
      tests/test_harness_monitor_bridge.py \
      tests/test_harness_monitor_canary.py \
      tests/test_agent_harness_mvp.py \
      tests/test_agent_harness_terminal.py \
      tests/test_agent_harness_reconcile_cli.py \
      tests/test_agent_harness_managed.py \
      tests/test_agent_harness_parent.py \
      "$@"
    ;;
  *)
    echo "refusing unknown local-ci test tier: $tier" >&2
    exit 2
    ;;
esac
exec "${PYTHON:-python3}" -m pytest -q --disable-warnings "$@"
