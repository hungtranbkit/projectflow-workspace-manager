#!/usr/bin/env bash
set -euo pipefail
# Mandatory local-ci/verify contract for the Harness monitoring surface.
# This executes actual offline regression tests. It is not a substitute for
# the unrelated, currently baseline-failing repository-wide scripts/test.sh.
python3 -m pytest -q --disable-warnings \
  tests/test_harness_progress.py \
  tests/test_harness_trajectory.py \
  tests/test_harness_monitor_service.py \
  tests/test_harness_zgh_node_java_qa.py \
  tests/test_harness_monitor_bridge.py \
  tests/test_harness_monitor_canary.py \
  tests/test_agent_harness_mvp.py \
  tests/test_agent_harness_terminal.py \
  tests/test_agent_harness_reconcile_cli.py \
  tests/test_agent_harness_managed.py \
  tests/test_agent_harness_parent.py
