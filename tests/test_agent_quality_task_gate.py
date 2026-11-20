"""The opt-in gate must execute feature tests selected by policy, never by an agent."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import agent_quality_task_gate as gate  # noqa: E402


def test_real_monitor_task_is_pinned_to_real_feature_suite():
    policy, path = gate.load_task_policy("PROJECTFLOW-MONITOR-REPORT-PILOT")
    assert policy["code_files"] == ["scripts/harness_monitor_report.py"]
    assert policy["test_files"] == ["tests/test_harness_monitor_report.py"]
    assert policy["ui_required"] is False
    assert path.name == "agent_quality_tasks_pilot.json"


def test_unknown_task_cannot_select_easy_tests():
    with pytest.raises(ValueError, match="not authorized"):
        gate.load_task_policy("AGENT-MADE-UP-TASK")


def test_ui_requirement_cannot_be_dropped_via_cli(tmp_path):
    result = gate.run_task_gate("PROJECTFLOW-UI-NEGATIVE-PILOT", report_dir=tmp_path)
    assert result["status"] == "FAIL"
    assert result["ui_required"] is True
    assert result["test_exit_code"] is None
    assert any("screenshot" in reason for reason in result["failure_reasons"])


def _make_policy_fixture(tmp_path: Path, test_file: str) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "ok.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (tmp_path / "config" / "agent_quality_tasks_pilot.json").write_text(
        json.dumps({"schema_version": 1, "tasks": {"X": {
            "code_files": ["scripts/ok.py"], "test_files": [test_file], "ui_required": False
        }}}), encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize("test_file", [
    "scripts/ok.py", "../outside.py", "tests/missing.py"
])
def test_rejects_untrusted_test_path(tmp_path, test_file):
    root = _make_policy_fixture(tmp_path, test_file)
    with pytest.raises(ValueError):
        gate.load_task_policy("X", root)


def test_rejects_invalid_policy_schema(tmp_path):
    root = _make_policy_fixture(tmp_path, "tests/test_ok.py")
    (root / "config" / "agent_quality_tasks_pilot.json").write_text('{"schema_version":0,"tasks":{}}')
    with pytest.raises(ValueError, match="schema"):
        gate.load_task_policy("X", root)


def test_selfcheck_runs_pinned_tests_and_writes_hashes(tmp_path):
    report = gate.run_task_gate("PROJECTFLOW-QUALITY-SELFTEST", report_dir=tmp_path)
    assert report["status"] == "PASS", report["failure_reasons"]
    assert report["test_command"][-1] == "tests/test_agent_quality_pilot.py"
    assert report["test_exit_code"] == 0
    assert report["policy_locked"] is True
    assert report["code_sha256"]["scripts/agent_quality_pilot.py"]
    assert report["test_sha256"]["tests/test_agent_quality_pilot.py"]
    saved = json.loads(Path(report["report_file"]).read_text())
    assert saved["status"] == "PASS"
    assert saved["runtime_scope"] == "PINNED_PILOT_TASK_TESTS_ONLY"
