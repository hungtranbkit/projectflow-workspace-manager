"""Opt-in REAL Docker QA acceptance, not part of routine unit/CI runs.

Run explicitly on an authorized isolated QA node with:
  PROJECTFLOW_QA_RUNTIME_IMAGE_ID=sha256:<64-hex> \
      python -m pytest -q -o addopts='' tests/test_quality_isolated_runtime.py
The runtime must use a local prebuilt image. No build, pull, merge, deploy,
unrelated container cleanup or public network access occurs in this test.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from scripts.agent_quality_isolated_runner import make_isolated_test_runner


def test_pinned_monitor_report_runs_inside_real_restricted_docker():
    image_id = os.environ.get("PROJECTFLOW_QA_RUNTIME_IMAGE_ID", "")
    if not image_id:
        pytest.skip("requires an explicit locally inspected immutable QA image ID")
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
    root = Path(__file__).resolve().parents[1]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, text=True,
        capture_output=True, check=True, timeout=10,
    )
    assert status.stdout.strip() == "", "QA candidate must be clean"
    result = make_isolated_test_runner(image_id)(
        [sys.executable, "-m", "pytest", "-q", "tests/test_harness_monitor_report.py"],
        root, {},
    )
    assert result.returncode == 0, (
        f"Isolated pytest exit={result.returncode}\n"
        f"stdout:\n{result.stdout[-3000:]}\n"
        f"stderr:\n{result.stderr[-3000:]}"
    )
    # PROJECT pytest.ini already adds -q; the runner adds -q again.
    # With -qq, pytest prints ten passing dots without a "10 passed" line.
    assert re.search(r"^\.{10}\s+\[100%\]$", result.stdout, re.MULTILINE), result.stdout[-3000:]
