"""Unit tests: managed hard-TTL guards and conservative owned extra cleanup."""
from pathlib import Path
import json
import subprocess
from datetime import datetime, timezone
import pytest

from scripts.harness_managed_exec import (
    ManagedHarness, archive_known_generated, unit_status,
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
    with pytest.raises(HarnessError, match="pytest"):
        manager.launch(main_goal="main", child_goal="child", expected="tests/missing.py",
                       repo=str(repo), executor="codex-task", ttl_seconds=20)
    git("-C", repo, "checkout", "-b", "main")
    with pytest.raises(HarnessError, match="harness/"):
        manager.launch(main_goal="main", child_goal="child", expected="tests/test_calc.py",
                       repo=str(repo), executor="codex-task", ttl_seconds=20)


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
