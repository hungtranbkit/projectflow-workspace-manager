"""Task detail renders Visual QA from the existing decision/evidence path."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_visual_qa_evidence import create_run, png


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()


def _task_workspace(client, repo: Path, *, opted_in: bool, repository_id: int | None = None):
    reference = repo / "docs" / "order.png"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(png(430, 932))
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: visual-panel}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\n  visual_qa: {command: 'true'}\n"
        "ci: {required: [preflight, test]}\n"
        "visual_qa:\n  enabled: true\n  reference: docs/order.png\n"
        "  route: http://127.0.0.1:4173/orders\n  viewports: [{width: 430, height: 932}]\n  threshold: 0.08\n",
        encoding="utf-8",
    )
    _git(repo, "add", "PROJECT.yaml", "docs/order.png")
    _git(repo, "commit", "-m", "add visual QA panel fixture contract")
    head = _git(repo, "rev-parse", "HEAD")

    db = client.app.state.db
    task_id = db.execute(
        "INSERT INTO tasks(slug,title,status,implementation_prompt,repo_scope_id) VALUES(?,?,?,?,?)",
        ("visual-panel", "Visual panel task", "ACTIVE", "UI preview" + ("\nVISUAL_QA_REQUIRED: true" if opted_in else ""), repository_id),
    )
    if repository_id is None:
        repository_id = db.execute(
            "INSERT INTO repositories(repo_name,repo_path,default_branch) VALUES(?,?,?)",
            ("visual-panel-fixture", str(repo), "main"),
        )
    branch, worktree, commit = client.app.state.git.create_agent(repo, "codex", f"visual-panel-{task_id}", "main")
    workspace_id = db.execute(
        "INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id,role,sandbox_profile) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (repository_id, "codex", "visual-panel", branch, str(worktree), "main", commit, commit, "CREATED", task_id, "UI", "NONE"),
    )
    # The fixture source is a committed worktree and the reference path is repo-local.
    assert commit == head
    return task_id, workspace_id, Path(worktree), head


def _add_visual_run(client, workspace_id: int, worktree: Path, head: str, *, tamper=False):
    db = client.app.state.db
    for stage in ("preflight", "test"):
        db.execute(
            "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,exit_code) VALUES(?,?,?,?,?,?,?)",
            ("agent", workspace_id, "true", stage, "PASS", head, 0),
        )
    run_id = db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,stdout_tail,stderr_tail) VALUES(?,?,?,?,?,?,?,?)",
        ("agent", workspace_id, "true", "visual_qa", "PASS", head, "", ""),
    )
    exp = {
        "run_id": run_id,
        "tested_commit": head,
        "repo_root": worktree,
        "contract": {
            "reference": {"430x932": "docs/order.png"},
            "route": "http://127.0.0.1:4173/orders",
            "viewports": [{"width": 430, "height": 932}],
            "threshold": 0.08,
        },
    }
    evidence_root = client.app.state.settings.state_dir / "visual-qa-evidence" / "agent" / str(workspace_id) / str(run_id)
    artifact_dir = create_run(evidence_root, exp, manifest_overrides={"runId": str(run_id)})
    from app.services.visual_qa_evidence import validate_visual_qa_run
    checked = validate_visual_qa_run(evidence_root, **exp)
    db.execute("UPDATE test_runs SET stdout_tail=? WHERE id=?", (f"PROJECTFLOW_VISUAL_EVIDENCE_SHA256={checked['manifest_sha256']}\n", run_id))
    if tamper:
        metrics_path = artifact_dir / "metrics.json"
        metrics = json.loads(metrics_path.read_text())
        metrics["percentDiff"] = 0.5
        metrics_path.write_text(json.dumps(metrics))
    return run_id


def test_opted_in_task_detail_shows_validated_contract_and_existing_test_action(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, _head = _task_workspace(client, repo, opted_in=True)

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert 'id="visual-qa"' in response.text
    assert "Bắt buộc theo Task" in response.text
    assert "http://127.0.0.1:4173/orders" in response.text
    assert "430x932" in response.text
    assert "docs/order.png" in response.text
    assert hashlib.sha256((worktree / "docs/order.png").read_bytes()).hexdigest() in response.text
    assert "Chưa ghi nhận — không đồng nghĩa đã được duyệt." in response.text
    assert "Chưa chạy" in response.text
    assert f"/api/workspaces/{workspace_id}/test" in response.text


def test_opted_out_task_explains_contract_does_not_activate_gate(client, git_repo):
    _root, repo = git_repo
    task_id, _workspace_id, _worktree, _head = _task_workspace(client, repo, opted_in=False)

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert "Không bắt buộc" in response.text
    assert "không tự bật gate" in response.text
    assert "Bắt buộc theo Task" not in response.text


def test_current_valid_run_displays_real_verified_metrics_and_log_link(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)
    # A later row from another SHA must not hide the current source result.
    client.app.state.db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,exit_code) VALUES(?,?,?,?,?,?,?)",
        ("agent", workspace_id, "true", "visual_qa", "FAIL", "different-stale-sha", 1),
    )

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert "Evidence đã xác minh" in response.text
    assert "0.000%" in response.text
    assert head in response.text
    assert f"/test-runs/{run_id}/log" in response.text


def test_stale_or_tampered_visual_evidence_is_not_presented_as_valid(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    _add_visual_run(client, workspace_id, worktree, head, tamper=True)

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert "Bằng chứng thiếu hoặc không hợp lệ" in response.text
    assert "0.000%" not in response.text
    assert client.app.state.decision.evaluate(task_id)["builders"][0]["tests_status"] == "FAIL"


def test_current_pass_without_manifest_digest_fails_closed_and_never_links_artifacts(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)
    client.app.state.db.execute("UPDATE test_runs SET stdout_tail='' WHERE id=?", (run_id,))

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert "Bằng chứng thiếu hoặc không hợp lệ" in response.text
    assert "0.000%" not in response.text
    assert "/test-runs/" + str(run_id) + "/log" in response.text
    assert "/visual-qa-evidence/" not in response.text
    assert client.app.state.decision.evaluate(task_id)["builders"][0]["tests_status"] == "FAIL"


def test_stale_run_and_invalid_contract_are_spelled_out(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, _head = _task_workspace(client, repo, opted_in=True)
    client.app.state.db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,exit_code) VALUES(?,?,?,?,?,?,?)",
        ("agent", workspace_id, "true", "visual_qa", "PASS", "old-source-sha", 0),
    )
    stale = client.get(f"/tasks/{task_id}")
    assert "STALE" in stale.text

    (worktree / "PROJECT.yaml").write_text("visual_qa: []\n", encoding="utf-8")
    invalid = client.get(f"/tasks/{task_id}")
    assert "Contract Visual QA không hợp lệ" in invalid.text
    assert "Evidence đã xác minh" not in invalid.text


def test_opted_in_task_without_repo_contract_is_blocked_visibly(client, git_repo):
    _root, repo = git_repo
    task_id, _workspace_id, worktree, _head = _task_workspace(client, repo, opted_in=True)
    (worktree / "PROJECT.yaml").write_text("commands: {test: {command: 'true'}}\n", encoding="utf-8")

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert "Task đã opt-in nhưng repo chưa có contract Visual QA hợp lệ" in response.text
    assert "Bắt buộc theo Task" in response.text


def test_task_detail_escapes_untrusted_title_in_visual_panel_page(client, git_repo):
    _root, repo = git_repo
    task_id, _workspace_id, _worktree, _head = _task_workspace(client, repo, opted_in=True)
    client.app.state.db.execute("UPDATE tasks SET title=? WHERE id=?", ("<script>alert(1)</script>", task_id))

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


@pytest.mark.parametrize("artifact", ["reference.png", "actual.png", "diff.png", "overlay.png"])
def test_valid_visual_artifact_route_returns_exact_verified_bytes_and_private_headers(client, git_repo, artifact):
    _root, repo = git_repo
    task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)
    expected = (client.app.state.settings.state_dir / "visual-qa-evidence" / "agent"
                / str(workspace_id) / str(run_id) / "430x932" / artifact).read_bytes()

    response = client.get(f"/test-runs/{run_id}/visual/430x932/{artifact}")

    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert "attachment" not in response.headers.get("content-disposition", "")


@pytest.mark.parametrize("viewport,artifact", [
    ("../430x932", "actual.png"), ("430x932", "../../metrics.json"),
    ("9999x9999", "actual.png"), ("430x932", "report.md"),
])
def test_visual_artifact_route_rejects_unlisted_viewport_and_artifact(client, git_repo, viewport, artifact):
    _root, repo = git_repo
    _task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)

    response = client.get(f"/test-runs/{run_id}/visual/{viewport}/{artifact}")

    assert response.status_code == 404


def test_visual_artifact_route_fails_closed_for_tamper_stale_dirty_and_opt_out(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)
    evidence = client.app.state.settings.state_dir / "visual-qa-evidence" / "agent" / str(workspace_id) / str(run_id) / "430x932" / "actual.png"
    evidence.write_bytes(b"tampered")
    assert client.get(f"/test-runs/{run_id}/visual/430x932/actual.png").status_code == 404

    evidence.write_bytes(png(430, 932))
    (worktree / "README.md").write_text("dirty tracked source\n", encoding="utf-8")
    assert client.get(f"/test-runs/{run_id}/visual/430x932/actual.png").status_code == 404
    (worktree / "README.md").write_text("base\n", encoding="utf-8")

    client.app.state.db.execute("UPDATE tasks SET implementation_prompt='backend only' WHERE id=?", (task_id,))
    assert client.get(f"/test-runs/{run_id}/visual/430x932/actual.png").status_code == 404


def test_visual_artifact_route_rejects_symlink_and_missing_manifest_digest(client, git_repo, tmp_path):
    _root, repo = git_repo
    _task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)
    artifact = (client.app.state.settings.state_dir / "visual-qa-evidence" / "agent"
                / str(workspace_id) / str(run_id) / "430x932" / "actual.png")
    outside = tmp_path / "not-owned.png"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(outside)
    assert client.get(f"/test-runs/{run_id}/visual/430x932/actual.png").status_code == 404

    artifact.unlink()
    artifact.write_bytes(png(430, 932))
    client.app.state.db.execute("UPDATE test_runs SET stdout_tail='' WHERE id=?", (run_id,))
    assert client.get(f"/test-runs/{run_id}/visual/430x932/actual.png").status_code == 404


def test_visual_artifact_route_rejects_newer_failed_run_and_clean_stale_sha(client, git_repo):
    _root, repo = git_repo
    _task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)
    client.app.state.db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,exit_code) "
        "VALUES('agent',?,'true','visual_qa','FAIL',?,1)", (workspace_id, head),
    )
    assert client.get(f"/test-runs/{run_id}/visual/430x932/actual.png").status_code == 404

    # The prior PASS is not served after a new clean source commit.
    client.app.state.db.execute("DELETE FROM test_runs WHERE workspace_id=? AND stage='visual_qa' AND status='FAIL'", (workspace_id,))
    (worktree / "README.md").write_text("next committed source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "new visual source"], check=True, capture_output=True)
    assert client.get(f"/test-runs/{run_id}/visual/430x932/actual.png").status_code == 404


def test_task_page_embeds_only_authenticated_urls_for_current_verified_visual_images(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    for artifact in ("reference.png", "actual.png", "diff.png", "overlay.png"):
        assert f"/test-runs/{run_id}/visual/430x932/{artifact}" in response.text
    assert 'alt="Actual 430x932"' in response.text
    assert 'alt="Diff 430x932"' in response.text


def test_image_route_uses_the_exact_run_workspace_within_a_task(client, git_repo):
    _root, repo = git_repo
    task_id, workspace_id, worktree, head = _task_workspace(client, repo, opted_in=True)
    run_id = _add_visual_run(client, workspace_id, worktree, head)
    branch, other_worktree, commit = client.app.state.git.create_agent(
        repo, "claude", f"visual-panel-other-{task_id}", "main"
    )
    other_id = client.app.state.db.execute(
        "INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id,role,sandbox_profile) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (client.app.state.db.one("SELECT id FROM repositories WHERE repo_path=?", (str(repo),))["id"],
         "claude", "visual-panel-other", branch, str(other_worktree), "main", commit, commit,
         "CREATED", task_id, "UI", "NONE"),
    )
    other_run_id = _add_visual_run(client, other_id, Path(other_worktree), commit)
    other_actual = png(430, 932, color=(200, 10, 30))
    other_actual_path = (client.app.state.settings.state_dir / "visual-qa-evidence" / "agent"
                         / str(other_id) / str(other_run_id) / "430x932" / "actual.png")
    other_actual_path.write_bytes(other_actual)
    # Rebuild valid manifest/metrics for the distinct workspace artifact.
    exp = {
        "run_id": other_run_id, "tested_commit": commit, "repo_root": Path(other_worktree),
        "contract": {"reference": {"430x932": "docs/order.png"}, "route": "http://127.0.0.1:4173/orders",
                     "viewports": [{"width": 430, "height": 932}], "threshold": 0.08},
    }
    from tests.test_visual_qa_evidence import create_run
    import shutil
    shutil.rmtree(other_actual_path.parent)
    create_run(other_actual_path.parent.parent, exp,
               manifest_overrides={"runId": str(other_run_id)},
               artifact_overrides={"actual.png": other_actual})
    from app.services.visual_qa_evidence import validate_visual_qa_run
    checked = validate_visual_qa_run(other_actual_path.parent.parent, **exp)
    client.app.state.db.execute(
        "UPDATE test_runs SET stdout_tail=? WHERE id=?",
        (f"PROJECTFLOW_VISUAL_EVIDENCE_SHA256={checked['manifest_sha256']}\n", other_run_id),
    )

    first = client.get(f"/test-runs/{run_id}/visual/430x932/actual.png")
    second = client.get(f"/test-runs/{other_run_id}/visual/430x932/actual.png")

    assert first.status_code == second.status_code == 200
    assert first.content != second.content
    assert second.content == other_actual


def test_visual_image_route_reuses_required_authz_and_denies_other_tenant(git_repo, tmp_path):
    import logging
    from fastapi.testclient import TestClient
    from tests.test_b03_authz import (
        _accept, _bootstrap, _create_org, _invite, _link_repo, _second_repo, auth_client,
    )

    _root, repo = git_repo
    captured = []
    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())
    handler = Capture()
    loggers = [logging.getLogger("projectflow.auth"), logging.getLogger("projectflow.email")]
    for logger in loggers:
        logger.addHandler(handler)
    try:
        auth_root = tmp_path / "auth-mode-required"
        auth_root.mkdir()
        owner = auth_client(_root, auth_root)
        _bootstrap(owner, captured)
        org_a = _create_org(owner, "Visual A")
        repository_id = _link_repo(owner, org_a, repo, "visual-a")
        task_id, workspace_id, worktree, head = _task_workspace(
            owner, repo, opted_in=True, repository_id=repository_id
        )
        run_id = _add_visual_run(owner, workspace_id, worktree, head)
        outsider_org = _create_org(owner, "Visual B")
        repo_b = _second_repo(_root, "visual-b-repo")
        _link_repo(owner, outsider_org, repo_b, "visual-b")
        invitation = _invite(owner, outsider_org, "visual-outsider@example.test", "OWNER", captured)
        outsider = _accept(owner.app, invitation)
        anonymous = TestClient(owner.app)
        expected = (owner.app.state.settings.state_dir / "visual-qa-evidence" / "agent"
                    / str(workspace_id) / str(run_id) / "430x932" / "actual.png").read_bytes()

        allowed = owner.get(f"/test-runs/{run_id}/visual/430x932/actual.png")
        unauthenticated = anonymous.get(f"/test-runs/{run_id}/visual/430x932/actual.png")
        foreign_tenant = outsider.get(f"/test-runs/{run_id}/visual/430x932/actual.png")

        assert allowed.status_code == 200 and allowed.content == expected
        assert unauthenticated.status_code == 401
        assert foreign_tenant.status_code == 404
        assert task_id > 0
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
