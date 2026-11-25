"""Integration verification must read the contract from its exact Git probe."""
from __future__ import annotations

import subprocess

import pytest

from app.services.git_workspace import GitWorkspaceService
from app.services.integration_service import IntegrationService


class TaskDB:
    def __init__(self, task=None):
        self.task = task

    def one(self, sql, args=()):
        return self.task


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, text=True, capture_output=True).stdout.strip()


def project_yaml(command: str, *, visual: str = "") -> str:
    return (
        "schema_version: 1\nproject: {code: probe}\nsource: {root: .}\n"
        f"commands:\n  test: {{command: {command!r}, working_directory: ., timeout_seconds: 19}}\n"
        "ci: {required: [test]}\n" + visual
    )


def make_repo(tmp_path, yaml_text):
    root = tmp_path / "workspace"
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Integration contract test")
    (repo / "PROJECT.yaml").write_text(yaml_text, encoding="utf-8")
    (repo / "sentinel.txt").write_text("candidate\n", encoding="utf-8")
    (repo / "reference.png").write_bytes(b"disposable-reference-fixture")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "canonical contract")
    canonical = git(repo, "rev-parse", "HEAD")
    return root, repo, canonical


def verify(root, repo, commit, task=None):
    seen = []

    def runner(argv, cwd, timeout):
        result = subprocess.run(argv, cwd=cwd, check=False, text=True, capture_output=True, timeout=timeout)
        seen.append((str(cwd), timeout, result.returncode, result.stdout, result.stderr))
        return result

    git_service = GitWorkspaceService(root, worktree_root=root / ".worktrees")
    service = IntegrationService(TaskDB(task), None, None, None, git_service, runner=runner)
    result = service.verify_integrated_state(str(repo), commit, task_id=1 if task else None)
    return result, seen


def candidate_commit(repo, yaml_text=None):
    git(repo, "checkout", "-b", "candidate")
    if yaml_text is None:
        (repo / "PROJECT.yaml").unlink()
    else:
        (repo / "PROJECT.yaml").write_text(yaml_text, encoding="utf-8")
    git(repo, "add", "PROJECT.yaml")
    git(repo, "commit", "-m", "candidate contract")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    return candidate


def test_verification_uses_candidate_contract_and_timeout(tmp_path):
    root, repo, _ = make_repo(tmp_path, project_yaml("printf canonical"))
    candidate = candidate_commit(repo, project_yaml("printf candidate"))

    result, seen = verify(root, repo, candidate)

    assert result["passed"]
    assert [(item[1], item[3].strip()) for item in seen] == [(19, "candidate")]
    assert ".worktrees" in seen[0][0]


@pytest.mark.parametrize(
    "candidate_yaml",
    [
        None,
        "schema_version: 1\nproject: {code: candidate}\nsource: {root: .}\ncommands:\n  test: {command: 'printf should-not-run'}\n  visual_qa: {command: 'printf should-not-run'}\nci: {required: [test]}\nvisual_qa: {enabled: true}\n",
    ],
    ids=["missing-project-contract", "invalid-visual-contract"],
)
def test_required_visual_qa_fails_closed_on_candidate_contract(tmp_path, candidate_yaml):
    valid_visual = (
        "schema_version: 1\nproject: {code: canonical}\nsource: {root: .}\n"
        "commands:\n  test: {command: 'printf canonical'}\n  visual_qa: {command: 'printf visual'}\n"
        "ci: {required: [test]}\n"
        "visual_qa:\n  enabled: true\n  route: http://127.0.0.1:8765/\n"
        "  viewports: [{width: 430, height: 932}]\n  reference: reference.png\n  threshold: 0.08\n"
    )
    root, repo, _ = make_repo(tmp_path, valid_visual)
    candidate = candidate_commit(repo, candidate_yaml)
    task = {"implementation_prompt": "VISUAL_QA_REQUIRED: true"}

    result, seen = verify(root, repo, candidate, task)

    assert not result["passed"]
    assert result["results"][0]["stage"] == "visual_qa"
    assert "invalid or missing" in result["results"][0]["stderr"]
    assert seen == []


def test_dirty_canonical_contract_cannot_replace_candidate_and_legacy_nonvisual_still_runs(tmp_path):
    root, repo, _ = make_repo(tmp_path, project_yaml("printf candidate"))
    candidate = git(repo, "rev-parse", "HEAD")
    (repo / "PROJECT.yaml").write_text(project_yaml("printf dirty-canonical"), encoding="utf-8")
    result, seen = verify(root, repo, candidate)
    assert result["passed"]
    assert [item[3].strip() for item in seen] == ["candidate"]

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    git(legacy, "init", "-b", "main")
    git(legacy, "config", "user.email", "test@example.invalid")
    git(legacy, "config", "user.name", "Integration contract test")
    (legacy / "README.md").write_text("legacy\n", encoding="utf-8")
    (legacy / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: legacy}\nsource: {root: .}\ncommands: {test: {command: 'printf legacy'}}\nci: {required: [test]}\n", encoding="utf-8")
    git(legacy, "add", ".")
    git(legacy, "commit", "-m", "legacy contract")
    legacy_root = tmp_path / "legacy-workspace"
    legacy_root.mkdir()
    # Place the real repository below the GitWorkspaceService's allowed root.
    moved = legacy_root / "repo"
    legacy.rename(moved)
    legacy_head = git(moved, "rev-parse", "HEAD")
    legacy_result, legacy_seen = verify(legacy_root, moved, legacy_head)
    assert legacy_result["passed"]
    assert [item[3].strip() for item in legacy_seen] == ["legacy"]
