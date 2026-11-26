from pathlib import Path

import pytest

from app.services.visual_qa_contract import (
    load_visual_qa_contract,
    task_requires_visual_qa,
    visual_qa_prompt_lines,
)


def write_contract(root: Path, block: str = "") -> None:
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs/reference.png").write_bytes(b"png fixture")
    (root / "PROJECT.yaml").write_text(
        "commands:\n  test:\n    command: pytest\n  visual_qa:\n    command: python tools/visual-qa.py\n"
        + block,
        encoding="utf-8",
    )


def test_visual_qa_contract_is_absent_by_default(tmp_path):
    write_contract(tmp_path)

    assert load_visual_qa_contract(tmp_path) is None


def test_visual_qa_contract_resolves_only_repo_local_reference(tmp_path):
    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/app/store-minh-anh/orders\n"
        "  viewports:\n"
        "    - {width: 430, height: 932}\n"
        "  threshold: 0.08\n",
    )

    contract = load_visual_qa_contract(tmp_path)

    assert contract["reference"] == {"430x932": "docs/reference.png"}
    assert contract["viewports"] == [{"width": 430, "height": 932}]


def test_multiple_viewports_require_a_reference_for_each_size(tmp_path):
    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/app/store-minh-anh/orders\n"
        "  viewports: [{width: 430, height: 932}, {width: 768, height: 1024}]\n"
        "  threshold: 0.08\n",
    )

    with pytest.raises(ValueError, match="require a reference for each viewport"):
        load_visual_qa_contract(tmp_path)


@pytest.mark.parametrize(
    "block, message",
    [
        ("visual_qa: []\n", "must be a mapping"),
        ("visual_qa:\n  enabled: true\n  reference: ../secret.png\n  route: http://127.0.0.1:4173/x\n  viewports: [{width: 430, height: 932}]\n", "repo-relative"),
        ("visual_qa:\n  enabled: true\n  reference: missing.png\n  route: http://127.0.0.1:4173/x\n  viewports: [{width: 430, height: 932}]\n", "does not exist"),
        (
            "visual_qa:\n  enabled: true\n  reference: docs/reference.png\n"
            "  route: https://example.com\n",
            "loopback",
        ),
    ],
)
def test_invalid_visual_qa_contract_fails_closed(tmp_path, block, message):
    write_contract(tmp_path, block)

    with pytest.raises(ValueError, match=message):
        load_visual_qa_contract(tmp_path)


def test_visual_qa_task_marker_is_explicit_and_backend_tasks_stay_opted_out():
    assert task_requires_visual_qa({"implementation_prompt": "UI change\nVISUAL_QA_REQUIRED: true"})
    assert not task_requires_visual_qa({"implementation_prompt": "Backend API migration"})


def test_visual_qa_stage_is_added_only_for_explicit_task_opt_in(tmp_path):
    from app.services.project_contract import load_contract

    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/app/store-minh-anh/orders\n"
        "  viewports: [{width: 430, height: 932}]\n"
        "  threshold: 0.08\n",
    )

    assert [row[0] for row in load_contract(tmp_path)] == ["test"]
    assert [row[0] for row in load_contract(tmp_path, visual_qa_required=True)] == ["test", "visual_qa"]


def test_task_opt_in_without_project_contract_fails_closed(tmp_path):
    write_contract(tmp_path)

    from app.services.project_contract import ContractError, load_contract

    with pytest.raises(ContractError, match="has no enabled visual_qa contract"):
        load_contract(tmp_path, visual_qa_required=True)


def test_prompt_adds_visual_qa_instructions_only_for_marked_ui_task(tmp_path):
    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/app/store-minh-anh/orders\n"
        "  viewports: [{width: 430, height: 932}]\n"
        "  threshold: 0.08\n",
    )

    assert not visual_qa_prompt_lines({"implementation_prompt": "Backend task"}, tmp_path)
    lines = visual_qa_prompt_lines(
        {"implementation_prompt": "Update screen\nVISUAL_QA_REQUIRED: true"}, tmp_path
    )
    assert "## VISUAL QA (required by this Task)" in lines
    assert "- Immutable references by viewport: 430x932: docs/reference.png." in lines


def test_marked_task_prompt_explains_missing_visual_qa_config(tmp_path):
    write_contract(tmp_path)

    lines = visual_qa_prompt_lines(
        {"implementation_prompt": "UI fix\nVISUAL_QA_REQUIRED: true"}, tmp_path
    )

    assert any("no enabled visual_qa contract" in line for line in lines)


def test_visual_qa_prompt_does_not_reference_bytes_hidden_from_git_status(tmp_path):
    import subprocess
    from app.services.git_workspace import GitWorkspaceService
    from tests.test_visual_qa_evidence import png

    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/orders\n"
        "  viewports: [{width: 430, height: 932}]\n"
        "  threshold: 0.08\n",
    )
    (tmp_path / "docs/reference.png").write_bytes(png(430, 932))
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Visual QA Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "visual-qa-test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "PROJECT.yaml", "docs/reference.png"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit prompt reference"], cwd=tmp_path, check=True, capture_output=True)
    git = GitWorkspaceService(tmp_path)
    task = {"implementation_prompt": "UI task\nVISUAL_QA_REQUIRED: true"}
    lines = visual_qa_prompt_lines(task, tmp_path, git=git)
    assert any("Immutable references by viewport" in line for line in lines)

    subprocess.run(["git", "update-index", "--skip-worktree", "docs/reference.png"], cwd=tmp_path, check=True)
    (tmp_path / "docs/reference.png").write_bytes(png(430, 932, color=(0, 0, 0)))
    assert git.source_state(tmp_path)["clean"] is True
    rejected = visual_qa_prompt_lines(task, tmp_path, git=git)

    assert any("contract is invalid" in line for line in rejected)
    assert not any("Immutable references by viewport" in line for line in rejected)


def test_test_runner_requires_visual_stage_only_for_marked_task(tmp_path):
    import subprocess
    from app.services.git_workspace import GitWorkspaceService
    from app.services.test_runner import TestRunner
    from tests.test_visual_qa_evidence import png

    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/app/store-minh-anh/orders\n"
        "  viewports: [{width: 430, height: 932}]\n"
        "  threshold: 0.08\n",
    )
    (tmp_path / "docs/reference.png").write_bytes(png(430, 932))
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Visual QA Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "visual-qa-test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "PROJECT.yaml", "docs/reference.png"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit visual contract fixture"], cwd=tmp_path, check=True, capture_output=True)

    class DB:
        task = {"implementation_prompt": "UI task\nVISUAL_QA_REQUIRED: true"}

        def one(self, sql, _params):
            return self.task if "agent_workspaces" in sql else None

    runner = TestRunner(DB(), git=GitWorkspaceService(tmp_path), runner=None)

    assert [row[0] for row in runner.stages_for("agent", 1, tmp_path)] == ["test", "visual_qa"]
    DB.task = {"implementation_prompt": "Backend task"}
    assert [row[0] for row in runner.stages_for("agent", 1, tmp_path)] == ["test"]


@pytest.mark.parametrize("ignored_path", ["PROJECT.yaml", "docs/reference.png"])
def test_test_runner_rejects_visual_inputs_absent_from_tested_git_tree(tmp_path, ignored_path):
    import subprocess
    from app.services.git_workspace import GitWorkspaceService
    from app.services.project_contract import ContractError
    from app.services.test_runner import TestRunner
    from tests.test_visual_qa_evidence import png

    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/orders\n"
        "  viewports: [{width: 430, height: 932}]\n"
        "  threshold: 0.08\n",
    )
    reference = tmp_path / "docs/reference.png"
    reference.write_bytes(png(430, 932))
    (tmp_path / ".gitignore").write_text(ignored_path + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Visual QA Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "visual-qa-test@example.invalid"], cwd=tmp_path, check=True)
    if ignored_path == "PROJECT.yaml":
        subprocess.run(["git", "add", ".gitignore", "docs/reference.png"], cwd=tmp_path, check=True)
    else:
        subprocess.run(["git", "add", ".gitignore", "PROJECT.yaml"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit visual QA source fixture"], cwd=tmp_path, check=True, capture_output=True)
    if ignored_path == "docs/reference.png":
        reference.write_bytes(png(430, 932))

    class DB:
        task = {"implementation_prompt": "UI task\nVISUAL_QA_REQUIRED: true"}

        def one(self, sql, _params):
            return self.task if "agent_workspaces" in sql else None

    assert GitWorkspaceService(tmp_path).source_state(tmp_path)["clean"] is True
    runner = TestRunner(DB(), GitWorkspaceService(tmp_path), runner=None)
    with pytest.raises(ContractError):
        runner.stages_for("agent", 1, tmp_path)


def test_task_decision_requires_current_visual_qa_evidence_for_opted_in_task(tmp_path):
    import subprocess
    from app.services.git_workspace import GitWorkspaceService
    from app.services.task_decision_service import TaskDecisionService
    from tests.test_visual_qa_evidence import png

    write_contract(
        tmp_path,
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/app/store-minh-anh/orders\n"
        "  viewports: [{width: 430, height: 932}]\n"
        "  threshold: 0.08\n",
    )
    (tmp_path / "docs/reference.png").write_bytes(png(430, 932))
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Visual QA Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "visual-qa-test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "PROJECT.yaml", "docs/reference.png"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit visual decision fixture"], cwd=tmp_path, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, text=True, capture_output=True).stdout.strip()

    class DB:
        task = {"implementation_prompt": "UI task\nVISUAL_QA_REQUIRED: true"}
        rows = [{"stage": "test", "status": "PASS"}]

        def one(self, _sql, _params):
            return self.task

        def all(self, _sql, _params):
            return self.rows

    decision = TaskDecisionService(DB(), GitWorkspaceService(tmp_path))
    builder = {"id": 7, "task_id": 3, "head": head, "worktree_path": str(tmp_path)}

    assert decision.builder_tests_status(builder) == "NOT_RUN"
    DB.rows.append({"stage": "visual_qa", "status": "PASS"})
    assert decision.builder_tests_status(builder) == "FAIL"  # exit/status row without coordinator-bound evidence is not proof
    DB.rows[1]["status"] = "FAIL"
    assert decision.builder_tests_status(builder) == "FAIL"
