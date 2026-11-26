"""Task-scoped uploaded reference drafts remain private proposals, never gate input."""
from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_b03_authz import captured_logs, two_org_fixture
from tests.test_visual_qa_evidence import png
from tests.test_visual_qa_task_panel import _task_workspace


VIEWPORT = "430x932"
PNG = png(430, 932, color=(51, 92, 136))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()


def _upload(client, tid, wid, data=PNG, *, viewport=VIEWPORT, filename="order.png", note="Figma handoff: Order form frame"):
    return client.post(
        f"/api/tasks/{tid}/workspaces/{wid}/visual-reference-drafts",
        data={"viewport": viewport, "design_note": note},
        files={"reference_png": (filename, data, "image/png")},
        follow_redirects=False,
    )


def _draft(client, tid, wid):
    return client.app.state.db.one(
        "SELECT * FROM work_products WHERE task_id=? AND kind='UI_UX_DESIGN' AND status='DRAFT' "
        "AND json_extract(content_metadata,'$.draft_kind')='visual-reference' "
        "AND json_extract(content_metadata,'$.workspace_id')=? ORDER BY id DESC LIMIT 1",
        (tid, wid),
    )


def test_task_upload_creates_private_unapproved_draft_and_preview_download(client, git_repo):
    _root, repo = git_repo
    tid, wid, worktree, head = _task_workspace(client, repo, opted_in=False)
    before_contract = _git(worktree, "rev-parse", "HEAD:PROJECT.yaml")
    result = _upload(client, tid, wid)

    assert result.status_code == 303
    draft = _draft(client, tid, wid)
    assert draft["project_id"] is None
    assert draft["change_id"] is None
    assert draft["content_ref"].startswith("visual-qa-drafts/")
    assert re.fullmatch(r"visual-qa-drafts/[0-9a-f]{32}\.png", draft["content_ref"])
    assert draft["content_digest"] == hashlib.sha256(PNG).hexdigest()
    metadata = json.loads(draft["content_metadata"])
    assert metadata == {
        "schema": "visual-reference-draft-v1",
        "draft_kind": "visual-reference",
        "approval": "UNREVIEWED",
        "workspace_id": wid,
        "repository_id": client.app.state.db.one("SELECT repository_id FROM agent_workspaces WHERE id=?", (wid,))["repository_id"],
        "source_commit": head,
        "source_clean": True,
        "viewport": VIEWPORT,
        "width": 430,
        "height": 932,
        "sha256": hashlib.sha256(PNG).hexdigest(),
        "design_note": "Figma handoff: Order form frame",
    }
    root = client.app.state.settings.state_dir / "visual-qa-drafts"
    stored = root / Path(draft["content_ref"]).name
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stored.read_bytes() == PNG

    preview = client.get(f"/tasks/{tid}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/preview")
    download = client.get(f"/tasks/{tid}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/download")
    assert preview.status_code == download.status_code == 200
    assert preview.content == download.content == PNG
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert preview.headers["cache-control"] == "private, no-store"
    assert "inline" in preview.headers["content-disposition"]
    assert "attachment" in download.headers["content-disposition"]
    assert "order.png" not in download.headers["content-disposition"]

    page = client.get(f"/tasks/{tid}")
    assert "Ảnh thiết kế nháp — chưa được duyệt" in page.text
    assert "Chưa ghi nhận — không đồng nghĩa đã được duyệt" in page.text
    assert _git(worktree, "rev-parse", "HEAD:PROJECT.yaml") == before_contract
    assert _git(worktree, "rev-parse", "HEAD") == head
    assert client.app.state.decision.evaluate(tid)["visual_qa_required"] is False
    assert _git(worktree, "status", "--porcelain") == ""


def test_visual_draft_upload_form_is_available_without_enabling_visual_gate(client, git_repo):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    page = client.get(f"/tasks/{tid}")
    assert page.status_code == 200
    assert f"/api/tasks/{tid}/workspaces/{wid}/visual-reference-drafts" in page.text
    assert 'name="reference_png"' in page.text
    assert 'name="viewport"' in page.text
    assert 'name="design_note"' in page.text
    assert "Ảnh chưa được duyệt" in page.text
    assert client.app.state.decision.evaluate(tid)["visual_qa_required"] is False


@pytest.mark.parametrize("case", ["bad-crc", "truncated", "svg", "wrong-dimensions"])
def test_invalid_images_do_not_create_drafts(client, git_repo, case):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    image, viewport, filename = PNG, VIEWPORT, "image.png"
    if case == "bad-crc":
        image = PNG[:-5] + bytes([PNG[-5] ^ 0x40]) + PNG[-4:]
    elif case == "truncated":
        image = PNG[:-10]
    elif case == "svg":
        image, filename = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "design.svg"
    elif case == "wrong-dimensions":
        image = png(768, 1024)
    result = _upload(client, tid, wid, image, viewport=viewport, filename=filename)
    assert result.status_code == 422
    assert _draft(client, tid, wid) is None


def test_viewport_is_allowlisted_and_upload_body_is_bounded(client, git_repo):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    unsupported = _upload(client, tid, wid, viewport="9999x9999")
    assert unsupported.status_code == 422
    too_large = _upload(client, tid, wid, b"x" * (8 * 1024 * 1024 + 1))
    assert too_large.status_code == 413
    request_too_large = _upload(client, tid, wid, b"x" * (8 * 1024 * 1024 + 64 * 1024 + 1))
    assert request_too_large.status_code == 413
    assert _draft(client, tid, wid) is None


def test_malicious_client_filename_is_never_used_as_storage_path(client, git_repo):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    result = _upload(client, tid, wid, filename="../../PROJECT.yaml")
    assert result.status_code == 303
    draft = _draft(client, tid, wid)
    assert draft["content_ref"].startswith("visual-qa-drafts/")
    assert ".." not in draft["content_ref"]
    assert client.get(f"/tasks/{tid}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/download").content == PNG


def test_design_note_is_escaped_in_task_page(client, git_repo):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    assert _upload(client, tid, wid, note="<script>not executable</script>").status_code == 303
    page = client.get(f"/tasks/{tid}")
    assert "&lt;script&gt;not executable&lt;/script&gt;" in page.text
    assert "<script>not executable</script>" not in page.text


def test_draft_read_requires_matching_task_and_worktree_and_rejects_tamper(client, git_repo):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    assert _upload(client, tid, wid).status_code == 303
    draft = _draft(client, tid, wid)
    other_tid = client.app.state.db.execute(
        "INSERT INTO tasks(slug,title,status,implementation_prompt) VALUES(?,?,?,?)",
        ("other-task", "Other task", "ACTIVE", "UI draft test"),
    )
    wrong_scope = client.get(f"/tasks/{other_tid}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/preview")
    assert wrong_scope.status_code == 404

    root = client.app.state.settings.state_dir / "visual-qa-drafts"
    stored = root / Path(draft["content_ref"]).name
    original = stored.read_bytes()
    stored.write_bytes(PNG[:-1] + b"x")
    assert client.get(f"/tasks/{tid}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/preview").status_code == 404
    stored.unlink()
    stored.symlink_to(root / "missing-target")
    assert client.get(f"/tasks/{tid}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/download").status_code == 404
    stored.unlink()
    stored.write_bytes(original)


def test_duplicate_draft_is_rejected_without_second_record(client, git_repo):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    assert _upload(client, tid, wid).status_code == 303
    duplicate = _upload(client, tid, wid)
    assert duplicate.status_code == 409
    rows = client.app.state.db.all(
        "SELECT id FROM work_products WHERE task_id=? AND kind='UI_UX_DESIGN' "
        "AND json_extract(content_metadata,'$.draft_kind')='visual-reference'", (tid,),
    )
    assert len(rows) == 1
    root = client.app.state.settings.state_dir / "visual-qa-drafts"
    assert len(list(root.glob("*.png"))) == 1


def test_workproduct_failure_cleans_only_new_upload_file(client, git_repo, monkeypatch):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)

    def fail_create(**_kwargs):
        raise RuntimeError("synthetic DB failure")

    monkeypatch.setattr(client.app.state.work_products, "create", fail_create)
    result = _upload(client, tid, wid)
    assert result.status_code == 500
    root = client.app.state.settings.state_dir / "visual-qa-drafts"
    assert list(root.glob("*.png")) == []


def test_workspace_change_during_upload_is_rejected_and_file_is_removed(client, git_repo, monkeypatch):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    original = client.app.state.git.source_state
    calls = 0

    def drift(path):
        nonlocal calls
        calls += 1
        state = original(path)
        if calls == 2:
            state["clean"] = False
        return state

    monkeypatch.setattr(client.app.state.git, "source_state", drift)
    result = _upload(client, tid, wid)
    assert result.status_code == 409
    assert _draft(client, tid, wid) is None
    root = client.app.state.settings.state_dir / "visual-qa-drafts"
    assert list(root.glob("*.png")) == []


def test_symlink_storage_root_is_rejected_without_writing_outside_state(client, git_repo, tmp_path):
    _root, repo = git_repo
    tid, wid, _worktree, _head = _task_workspace(client, repo, opted_in=False)
    state_dir = client.app.state.settings.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "outside"
    external.mkdir(mode=0o700)
    (state_dir / "visual-qa-drafts").symlink_to(external, target_is_directory=True)
    result = _upload(client, tid, wid)
    assert result.status_code == 500
    assert _draft(client, tid, wid) is None
    assert list(external.iterdir()) == []


def test_create_and_read_require_task_membership_and_csrf(two_org_fixture):
    f = two_org_fixture
    repo = Path(f["db"].one("SELECT repo_path FROM repositories WHERE id=?", (f["rid_a"],))["repo_path"])
    branch, worktree, commit = f["owner"].app.state.git.create_agent(repo, "codex", "visual-draft-auth", "main")
    wid = f["db"].execute(
        "INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,last_commit,status,task_id,role,sandbox_profile) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f["rid_a"], "codex", "visual draft auth", branch, str(worktree), "main", commit, commit,
         "CREATED", f["tid_a"], "UI", "NONE"),
    )
    endpoint = f"/api/tasks/{f['tid_a']}/workspaces/{wid}/visual-reference-drafts"
    no_csrf = f["member"].post(endpoint, data={"viewport": VIEWPORT, "design_note": "Design"},
                                 files={"reference_png": ("x.png", PNG, "image/png")}, follow_redirects=False)
    assert no_csrf.status_code == 403
    viewer_denied = f["viewer"].post(
        endpoint, data={"viewport": VIEWPORT, "design_note": "Design"},
        files={"reference_png": ("x.png", PNG, "image/png")}, follow_redirects=False,
    )
    assert viewer_denied.status_code == 403
    token = re.search(r'name="csrf_token"\s+value="([^"]+)"', f["member"].get("/account").text).group(1)
    accepted = f["member"].post(endpoint, data={"viewport": VIEWPORT, "design_note": "Design", "csrf_token": token},
                                 files={"reference_png": ("x.png", PNG, "image/png")}, follow_redirects=False)
    assert accepted.status_code == 303
    draft = _draft(f["owner"], f["tid_a"], wid)
    assert f["outsider"].get(
        f"/tasks/{f['tid_a']}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/preview"
    ).status_code == 404
    unauthenticated = TestClient(f["owner"].app)
    assert unauthenticated.get(
        f"/tasks/{f['tid_a']}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/preview"
    ).status_code == 401
    assert f["member"].get(
        f"/tasks/{f['tid_b']}/workspaces/{wid}/visual-reference-drafts/{draft['id']}/preview"
    ).status_code == 404
