"""Fail-closed monitor systemd health regression tests (no network / units)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from scripts.harness_monitor_service import reconcile


def _inputs(tmp_path: Path):
    db = tmp_path / "harness.db"
    from app.services.agent_harness_mvp import HarnessStore
    HarnessStore(db)
    py = tmp_path / "python"
    py.write_bytes(b"trusted")
    health = tmp_path / "monitor-health.json"
    return db, py, health


def _run_stage(call, *, core=None, bridge=None):
    is_core = "harness_reconcile.py" in " ".join(call)
    data = core if is_core else bridge
    if isinstance(data, Exception):
        raise data
    if isinstance(data, int):
        return SimpleNamespace(returncode=data, stdout="", stderr="failed")
    return SimpleNamespace(
        returncode=0,
        stdout=("HARNESS_RECONCILE=" if is_core else "HARNESS_MONITOR=")
        + json.dumps(data if data is not None else
                     ({"managed": [], "children": []} if is_core else
                      {"checked": 0, "results": []})) + "\n",
        stderr="")


def test_health_both_stages_pass_without_claiming_business_acceptance(tmp_path):
    db, py, output = _inputs(tmp_path)
    commands = []
    def runner(argv, **kwargs):
        commands.append((argv, kwargs))
        return _run_stage(argv)
    code, state = reconcile(
        db=db, health_file=output, python_core=py,
        python_bridge=py, runner=runner)
    assert code == 0
    assert state["healthy"] is True
    assert state["scheduler_healthy"] is True
    assert state["bridge_healthy"] is True
    assert state["business_accepted"] is False
    assert state["agent_verified"] is False
    assert json.loads(output.read_text())["healthy"] is True
    assert len(commands) == 2
    assert all(c[1]["timeout"] <= 23 for c in commands)


def test_bridge_failed_stage_makes_entire_service_fail(tmp_path):
    db, py, output = _inputs(tmp_path)
    def runner(argv, **kwargs):
        return _run_stage(argv, bridge=9)
    code, state = reconcile(
        db=db, health_file=output, python_core=py,
        python_bridge=py, runner=runner)
    assert code == 1
    assert state["scheduler_healthy"] is True
    assert state["bridge_healthy"] is False
    assert json.loads(output.read_text())["healthy"] is False


def test_observation_error_cannot_be_silently_ignored(tmp_path):
    db, py, output = _inputs(tmp_path)
    def runner(argv, **kwargs):
        return _run_stage(argv, bridge={
            "checked": 1, "results": [{"backend_state": "UNKNOWN",
                                        "error": "TASK_NOT_FOUND"}]})
    code, state = reconcile(
        db=db, health_file=output, python_core=py,
        python_bridge=py, runner=runner)
    assert code == 1
    assert state["bridge_observation_errors"] == 1
    assert state["business_accepted"] is False


def test_expected_central_namespace_unavailable_is_not_fabricated_pass(tmp_path):
    db, py, output = _inputs(tmp_path)
    def runner(argv, **kwargs):
        return _run_stage(argv, bridge={
            "checked": 1, "results": [{"backend_state": "UNAVAILABLE_LOCAL",
                                        "task_state": "DISPATCHED",
                                        "note": "central readback required"}]})
    code, state = reconcile(
        db=db, health_file=output, python_core=py,
        python_bridge=py, runner=runner)
    assert code == 0
    assert state["agent_verified"] is False
    assert state["business_accepted"] is False


def test_scheduler_failure_blocks_health_even_when_bridge_passes(tmp_path):
    db, py, output = _inputs(tmp_path)
    def runner(argv, **kwargs):
        return _run_stage(argv, core=13)
    code, state = reconcile(
        db=db, health_file=output, python_core=py,
        python_bridge=py, runner=runner)
    assert code == 1
    assert state["scheduler_healthy"] is False
    assert state["bridge_healthy"] is True


def test_timeout_persists_error_without_reporting_success(tmp_path):
    db, py, output = _inputs(tmp_path)
    def runner(argv, **kwargs):
        return _run_stage(argv, bridge=subprocess.TimeoutExpired(argv, 23))
    code, state = reconcile(
        db=db, health_file=output, python_core=py,
        python_bridge=py, runner=runner)
    assert code == 1
    assert state["healthy"] is False
    assert "TimeoutExpired" in state["errors"][0]


def test_core_managed_cleanup_failure_is_visible(tmp_path):
    db, py, output = _inputs(tmp_path)
    def runner(argv, **kwargs):
        return _run_stage(argv, core={"managed": [{"task_id": 2,
                                                   "error": "owned unit mismatch"}]})
    code, state = reconcile(
        db=db, health_file=output, python_core=py,
        python_bridge=py, runner=runner)
    assert code == 1
    assert state["managed_errors"] == 1


def test_health_file_must_live_inside_state_directory(tmp_path):
    db, py, _ = _inputs(tmp_path)
    with pytest.raises(ValueError, match="outside state dir"):
        reconcile(db=db, health_file=tmp_path.parent / "elsewhere.json",
                  python_core=py, python_bridge=py)
