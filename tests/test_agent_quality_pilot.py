"""Fast offline proof that the pilot passes good code and rejects unsafe code."""

import importlib.util
from pathlib import Path
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_quality_pilot.py"
_spec = importlib.util.spec_from_file_location("agent_quality_pilot", SCRIPT)
pilot = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pilot
_spec.loader.exec_module(pilot)


def test_lint_accepts_safe_code():
    assert pilot.lint_python("import subprocess\nsubprocess.run(['echo', 'ok'], check=True)\n") == []


def test_lint_blocks_dynamic_execution_and_shell():
    src = "import os, subprocess\neval('1+1')\nexec('a=1')\nos.system('echo test')\nsubprocess.run('echo test', shell=True)\n"
    rules = {item["rule"] for item in pilot.lint_python(src)}
    assert rules == {"NO_DYNAMIC_EXEC", "NO_OS_SYSTEM", "NO_SHELL_TRUE"}


def test_lint_blocks_silent_exception_and_syntax():
    assert pilot.lint_python("try:\n  run()\nexcept Exception:\n  pass\n")[0]["rule"] == "NO_SILENT_EXCEPT"
    assert pilot.lint_python("def !!!")[0]["rule"] == "PY_SYNTAX"


def test_gate_writes_pass_proof(tmp_path):
    (tmp_path / "scripts").mkdir()
    p = tmp_path / "safe.py"
    p.write_text("x = 1\n", encoding="utf8")
    report = pilot.quality_gate("pilot-ok", ["safe.py"], root=tmp_path,
                                report_dir=tmp_path / "evidence", test_command=[sys.executable, "-c", "print('PASS')"])
    assert report["status"] == "PASS"
    assert (tmp_path / "evidence" / "pilot-ok.json").is_file()
    assert "PASS" in (tmp_path / "evidence" / "pilot-ok.log").read_text()


def test_gate_rejects_lint_failure_without_running_tests(tmp_path):
    (tmp_path / "bad.py").write_text("eval('2+2')\n", encoding="utf8")
    report = pilot.quality_gate("pilot-bad", ["bad.py"], root=tmp_path,
                                report_dir=tmp_path / "evidence", test_command=[sys.executable, "-c", "print('SHOULD NOT RUN')"])
    assert report["status"] == "FAIL"
    assert report["test_exit_code"] is None
    assert report["lint_violations"][0]["rule"] == "NO_DYNAMIC_EXEC"


def test_gate_rejects_failed_tests(tmp_path):
    (tmp_path / "ok.py").write_text("pass\n", encoding="utf8")
    report = pilot.quality_gate("pilot-tests-fail", ["ok.py"], root=tmp_path,
                                report_dir=tmp_path / "evidence", test_command=[sys.executable, "-c", "raise SystemExit(7)"])
    assert report["status"] == "FAIL"
    assert report["test_exit_code"] == 7


def test_ui_requires_real_file(tmp_path):
    (tmp_path / "ok.py").write_text("pass\n", encoding="utf8")
    report = pilot.quality_gate("pilot-ui", ["ok.py"], root=tmp_path,
                                report_dir=tmp_path / "evidence", require_ui=True,
                                test_command=[sys.executable, "-c", "print('SHOULD NOT RUN')"])
    assert report["status"] == "FAIL"
    assert "screenshot" in report["failure_reasons"][0]


def test_protects_path_traversal(tmp_path):
    (tmp_path / "ok.py").write_text("pass\n")
    try:
        pilot.quality_gate("../bad", ["ok.py"], root=tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("traversal was not rejected")
    try:
        pilot.safe_repo_file("../outside.py", tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("outside repository accepted")
