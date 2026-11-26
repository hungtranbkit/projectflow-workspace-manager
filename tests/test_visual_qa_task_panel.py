"""Task detail renders Visual QA from the existing decision/evidence path."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from tests.test_visual_qa_evidence import create_run, png


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()


def _task_workspace(client, repo: Path, *, opted_in: bool):
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
        "INSERT INTO tasks(slug,title,status,implementation_prompt) VALUES(?,?,?,?)",
        ("visual-panel", "Visual panel task", "ACTIVE", "UI preview" + ("\nVISUAL_QA_REQUIRED: true" if opted_in else "")),
    )
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
