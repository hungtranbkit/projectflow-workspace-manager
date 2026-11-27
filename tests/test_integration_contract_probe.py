"""Integration verification must read the contract from its exact Git probe."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.git_workspace import GitWorkspaceService
from app.services.integration_service import IntegrationService
from tests.test_visual_qa_evidence import png
from tests.test_visual_qa_evidence import create_run
from app.services.visual_qa_contract import load_committed_visual_qa_contract


def test_app_wires_integration_visual_stage_to_test_runner_command_boundary(client):
    integration = client.app.state.integration_service
    test_runner = client.app.state.runner

    assert integration.visual_runner is test_runner.runner


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


def verify(root, repo, commit, task=None, visual_runner=None):
    seen = []

    def runner(argv, cwd, timeout):
        result = subprocess.run(argv, cwd=cwd, check=False, text=True, capture_output=True, timeout=timeout)
        seen.append((str(cwd), timeout, result.returncode, result.stdout, result.stderr))
        return result

    git_service = GitWorkspaceService(root, worktree_root=root / ".worktrees")
    service = IntegrationService(TaskDB(task), None, None, None, git_service,
                                 runner=runner, visual_runner=visual_runner)
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


def test_required_visual_integration_does_not_pass_from_exit_zero_without_evidence(tmp_path):
    root = tmp_path / "visual-workspace"
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Integration visual evidence test")
    visual_project = (
        "schema_version: 1\nproject: {code: probe}\nsource: {root: .}\n"
        "commands:\n  test: {command: 'true'}\n  visual_qa: {command: 'true'}\n"
        "ci: {required: [test]}\n"
        "visual_qa:\n  enabled: true\n  route: http://127.0.0.1:8765/orders\n"
        "  viewports: [{width: 320, height: 240}]\n  reference: reference.png\n  threshold: 0.08\n"
    )
    (repo / "PROJECT.yaml").write_text(visual_project, encoding="utf-8")
    (repo / "reference.png").write_bytes(png(320, 240))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "valid opted-in visual contract")
    tested_commit = git(repo, "rev-parse", "HEAD")
    task = {"implementation_prompt": "Verify merged UI\nVISUAL_QA_REQUIRED: true"}

    result, seen = verify(root, repo, tested_commit, task)

    assert not result["passed"], "exit 0 from `true` must not qualify required visual integration without validated evidence"
    visual = next(item for item in result["results"] if item["stage"] == "visual_qa")
    assert not visual["ok"]
    assert "evidence" in visual["stderr"].lower() or "runner" in visual["stderr"].lower()
    # The ordinary runner executed only the non-visual `test` stage. Missing
    # visual wiring must not silently fall back to its direct-host boundary.
    assert len(seen) == 1


def visual_repo(tmp_path):
    root = tmp_path / "visual-workspace"
    repo = root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Integration visual evidence test")
    (repo / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: probe}\nsource: {root: .}\n"
        "commands:\n  test: {command: 'true'}\n  visual_qa: {command: 'true'}\n"
        "ci: {required: [test, visual_qa]}\n"
        "visual_qa:\n  enabled: true\n  route: http://127.0.0.1:8765/orders\n"
        "  viewports: [{width: 320, height: 240}]\n  reference: reference.png\n  threshold: 0.08\n",
        encoding="utf-8",
    )
    (repo / "reference.png").write_bytes(png(320, 240))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "valid opted-in visual contract")
    return root, repo, git(repo, "rev-parse", "HEAD")


class VisualRunner:
    mandatory = False

    def __init__(self, action="empty"):
        self.action = action
        self.envs = []

    def run(self, command, worktree_root, working_dir, timeout, env=None):
        self.envs.append(dict(env or {}))
        if self.action == "empty":
            return subprocess.run(command, cwd=Path(worktree_root) / working_dir,
                                  shell=True, check=False, capture_output=True, text=True,
                                  timeout=timeout, env={**__import__("os").environ, **(env or {})})
        run_id = int(env["VISUAL_QA_RUN_ID"])
        commit = env["VISUAL_QA_TESTED_COMMIT"]
        repo = Path(worktree_root)
        contract = load_committed_visual_qa_contract(repo, GitWorkspaceService(repo.parent), commit)
        output = Path(env["VISUAL_QA_EVIDENCE_OUTPUT"])
        expected = {"run_id": run_id, "tested_commit": commit, "repo_root": repo, "contract": contract}
        if self.action == "valid":
            create_run(output, expected, manifest_overrides={"runId": str(run_id)})
        elif self.action == "stale":
            create_run(output, expected, manifest_overrides={"runId": str(run_id), "testedCommit": "f" * 40})
        elif self.action == "tampered":
            result = create_run(output, expected, manifest_overrides={"runId": str(run_id)})
            (result / "actual.png").write_bytes(b"tampered")
        return subprocess.CompletedProcess(command, 0, "fixture runner complete", "")


@pytest.mark.parametrize("action,expected_pass", [
    ("valid", True), ("empty", False), ("stale", False), ("tampered", False),
])
def test_visual_integration_requires_fresh_validated_run_artifacts(tmp_path, action, expected_pass):
    root, repo, commit = visual_repo(tmp_path)
    runner = VisualRunner(action)

    result, _ = verify(root, repo, commit, {"implementation_prompt": "VISUAL_QA_REQUIRED: true"}, runner)

    visual = next(item for item in result["results"] if item["stage"] == "visual_qa")
    assert result["passed"] is expected_pass
    assert visual["ok"] is expected_pass
    assert len(runner.envs) == 1
    assert runner.envs[0]["VISUAL_QA_TESTED_COMMIT"] == commit
    assert runner.envs[0]["VISUAL_QA_RUN_ID"].isdigit()
    if expected_pass:
        assert len(visual["evidence_sha256"]) == 64
        assert visual["evidence_run_id"] == runner.envs[0]["VISUAL_QA_RUN_ID"]
    else:
        assert "VISUAL_EVIDENCE_REJECTED" in visual["stderr"]


def test_visual_integration_run_id_collision_preserves_existing_directory(tmp_path, monkeypatch):
    import app.services.integration_service as integration_module

    root, repo, commit = visual_repo(tmp_path)
    git_service = GitWorkspaceService(root, worktree_root=root / ".worktrees")
    probe = git_service.create_baseline_probe(repo, commit)
    run_id = 4242
    output = probe / ".runtime" / "projectflow-visual-qa" / str(run_id)
    output.mkdir(parents=True)
    marker = output / "pre-existing.txt"
    marker.write_text("belongs to a different run", encoding="utf-8")
    monkeypatch.setattr(integration_module.secrets, "randbelow", lambda _: run_id - 1)
    runner = VisualRunner("valid")
    service = IntegrationService(TaskDB(), None, None, None, git_service, visual_runner=runner)

    try:
        result = service._verify_integrated_visual_stage(probe, commit, "true", ".", 19)
        assert not result["ok"]
        assert marker.read_text(encoding="utf-8") == "belongs to a different run"
        assert runner.envs == []
    finally:
        git_service.remove_baseline_probe(repo, probe)
