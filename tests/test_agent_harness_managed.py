"""Unit tests: managed hard-TTL guards and conservative owned extra cleanup."""
from pathlib import Path
import json
import os
import sys
import subprocess
from datetime import datetime, timezone
import pytest

from scripts.harness_managed_exec import (
    ManagedHarness, archive_known_generated, unit_status, validate_coding_delta, worker,
)
from app.services.agent_harness_mvp import HarnessError


def git(*args):
    return subprocess.run(["git", *map(str, args)], capture_output=True,
                          text=True, check=True, timeout=10)


@pytest.fixture()
def repo(tmp_path):
    p = tmp_path / "isolated"
    p.mkdir()
    git("-C", p, "init", "-b", "harness/isolated")
    (p / "calc.py").write_text("VALUE = 1\n")
    git("-C", p, "add", "calc.py")
    git("-C", p, "-c", "user.name=Harness QA", "-c",
        "user.email=harness-qa@localhost", "commit", "-m", "baseline")
    return p


def test_never_inspect_arbitrary_systemd_units():
    with pytest.raises(HarnessError, match="namespace"):
        unit_status("ssh.service")


def test_no_dirty_repo_or_over_24h_dispatch(repo, tmp_path):
    manager = ManagedHarness(tmp_path / "harness.db", tmp_path / "state")
    with pytest.raises(HarnessError, match="24h"):
        manager.launch(main_goal="main", child_goal="child", expected="abc",
                       repo=str(repo), executor="echo-probe", ttl_seconds=86401)
    (repo / "unexpected.py").write_text("data")
    with pytest.raises(HarnessError, match="dirty"):
        manager.launch(main_goal="main", child_goal="child", expected="abc",
                       repo=str(repo), executor="echo-probe", ttl_seconds=10)


def test_coding_requires_existing_pytest_and_isolated_branch(repo, tmp_path):
    manager = ManagedHarness(tmp_path / "harness.db", tmp_path / "state")
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text("def test_ok(): assert True\n")
    git("-C", repo, "add", "tests/test_calc.py")
    git("-C", repo, "-c", "user.name=Harness QA", "-c",
        "user.email=harness-qa@localhost", "commit", "-m", "test baseline")
    with pytest.raises(HarnessError, match="unsafe test"):
        manager.launch(main_goal="main", child_goal="child", expected="tests/test_missing.py",
                       repo=str(repo), executor="codex-task", ttl_seconds=20, allowed_files=["calc.py"])
    git("-C", repo, "checkout", "-b", "main")
    with pytest.raises(HarnessError, match="harness/"):
        manager.launch(main_goal="main", child_goal="child", expected="tests/test_calc.py",
                       repo=str(repo), executor="codex-task", ttl_seconds=20, allowed_files=["calc.py"])


def test_cleanup_archives_only_previously_untracked_known_files(repo, tmp_path):
    root = tmp_path / "state"
    (repo / "PROJECT_CONTEXT.md").write_text("generated note")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__/calc.cpython-314.pyc").write_bytes(b"compiled")
    assert archive_known_generated(repo, root, 7)
    assert not (repo / "PROJECT_CONTEXT.md").exists()
    manifest = json.loads((root / "retained/7/manifest.json").read_text())
    assert {item["path"] for item in manifest["archived"]} == {
        "PROJECT_CONTEXT.md", "__pycache__/calc.cpython-314.pyc"
    }
    assert git("-C", repo, "status", "--porcelain").stdout == ""


def test_cleanup_never_deletes_unknown_untracked_files(repo, tmp_path):
    unknown = repo / "valuable.txt"
    unknown.write_text("do not delete")
    (repo / "PROJECT_CONTEXT.md").write_text("safe to archive only if alone")
    assert not archive_known_generated(repo, tmp_path / "state", 9)
    assert unknown.read_text() == "do not delete"
    assert (repo / "PROJECT_CONTEXT.md").exists()


def test_coding_delta_rejects_test_file_tampering(repo):
    (repo / "tests").mkdir()
    target = repo / "tests/test_calc.py"
    target.write_text("def test_add(): assert True\n")
    git("-C", repo, "add", "tests/test_calc.py")
    git("-C", repo, "-c", "user.name=Harness QA", "-c",
        "user.email=harness-qa@localhost", "commit", "-m", "baseline test")
    target.write_text("def test_add(): assert False\n")
    valid, reason = validate_coding_delta(repo, "tests/test_calc.py", ["calc.py"])
    assert not valid
    assert "outside implementation" in reason


def test_coding_delta_rejects_config_and_unknown_untracked(repo):
    (repo / "calc.py").write_text("VALUE = 2\n")
    (repo / "settings.yml").write_text("dangerous: true\n")
    valid, reason = validate_coding_delta(repo, "tests/test_calc.py", ["calc.py"])
    assert not valid and "unknown untracked" in reason
    (repo / "settings.yml").unlink()
    (repo / ".github").mkdir()
    (repo / ".github/config.py").write_text("VALUE = 2")
    valid, reason = validate_coding_delta(repo, "tests/test_calc.py", ["calc.py"])
    assert not valid
    (repo / ".github/config.py").unlink()
    (repo / "calc.py").write_text("VALUE = 2\n")
    valid, reason = validate_coding_delta(repo, "tests/test_calc.py", ["calc.py"])
    assert valid, reason


def test_coding_delta_accepts_source_change_and_generated_context(repo):
    (repo / "calc.py").write_text("VALUE = 2\n")
    (repo / "PROJECT_CONTEXT.md").write_text("task report")
    valid, reason = validate_coding_delta(repo, "tests/test_calc.py", ["calc.py"])
    assert valid, reason


def test_coding_delta_rejects_staged_changes(repo):
    (repo / "calc.py").write_text("VALUE = 3\n")
    git("-C", repo, "add", "calc.py")
    valid, reason = validate_coding_delta(repo, "tests/test_calc.py", ["calc.py"])
    assert not valid and "staged" in reason


def test_malicious_agent_test_rewrite_never_commits(repo, tmp_path, monkeypatch):
    """Even a green pytest after rewriting the test must not count as PASS."""
    tests = repo / "tests"
    tests.mkdir()
    test_file = tests / "test_calc.py"
    test_file.write_text("from calc import VALUE\\n\\ndef test_value(): assert VALUE == 2\\n")
    git("-C", repo, "add", "tests/test_calc.py")
    git("-C", repo, "-c", "user.name=Harness QA", "-c",
        "user.email=harness-qa@localhost", "commit", "-m", "failing acceptance baseline")
    head_before = git("-C", repo, "rev-parse", "HEAD").stdout.strip()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = binaries / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\\n"
        "import pathlib,sys\\n"
        "pathlib.Path('calc.py').write_text('VALUE = 2\\\\n')\\n"
        "pathlib.Path('tests/test_calc.py').write_text('def test_value(): assert True\\\\n')\\n"
        "args=sys.argv\\n"
        "pathlib.Path(args[args.index('--output-last-message')+1]).write_text('fake reply')\\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ.get("PATH", ""))
    work = tmp_path / "run"
    work.mkdir()
    spec = {
        "task_id": 90, "unit": "harness-child-90-0123456789.service",
        "repo": str(repo), "run_dir": str(work), "executor": "codex-task",
        "expected": "tests/test_calc.py", "prompt": "Fix VALUE",
        "test_sha": __import__("hashlib").sha256(test_file.read_bytes()).hexdigest(),
        "approved_files": ["calc.py"],
    }
    spec_path = work / "job.json"
    spec_path.write_text(json.dumps(spec))
    assert worker(spec_path) != 0
    outcome = json.loads((work / "result.json").read_text())
    assert outcome.get("scope_verified") is False
    assert "outside implementation" in outcome.get("scope_refusal", "")
    assert git("-C", repo, "rev-parse", "HEAD").stdout.strip() == head_before
