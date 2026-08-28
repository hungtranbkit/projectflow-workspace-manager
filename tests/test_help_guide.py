"""Help V2: /help must work as a user-operating guide -- from "task mới"
to "sandbox sau merge" -- not architecture description. No docker needed;
this only exercises HTML rendering and contextual navigation."""
from __future__ import annotations


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"}, follow_redirects=False)
    assert r.status_code in (200, 303)


REQUIRED_SECTION_ANCHORS = [
    "quick-start", "tasks", "agent-workspaces", "open-agent", "sandbox", "sandbox-modes",
    "sandbox-auto", "global-sandbox", "task-detail-sandbox", "agent-done", "agent-ready-next", "integration",
    "integration-agent", "conflict", "cross-repo", "cross-repo-example", "source-stale",
    "restart-rebuild", "sandbox-status", "test-progress", "ready-for-main", "github-flow",
    "merge-cleanup", "stop-cleanup", "repositories", "no-sandbox-contract", "hardware",
    "scenarios", "troubleshoot", "nav-map",
]


def test_help_route_and_status(client):
    assert client.get("/help").status_code == 200


def test_help_contains_every_required_section_anchor(client):
    """Section 43: Quick Start, Task, Agent Workspace, Sandbox, Integration,
    Cross-repo, ESP/Kiosk, READY_FOR_MAIN, Cleanup, Repositories."""
    text = client.get("/help").text
    for anchor in REQUIRED_SECTION_ANCHORS:
        assert f'id="{anchor}"' in text, f"missing anchor #{anchor}"


def test_help_reads_as_operator_guide_not_architecture(client):
    text = client.get("/help").text
    # user-facing flow language, not developer/class-internal language
    for phrase in [
        "Hướng dẫn sử dụng Workspace Manager",
        "Bắt đầu nhanh",
        "Khi có bug hoặc task mới",
        "Agent Workspace là gì?",
        "Sandbox dùng để làm gì?",
        "Chọn Sandbox thế nào?",
        "Integration Agent làm gì?",
        "Nếu một task phải sửa nhiều repository",
        "Ví dụ: Fix Kiosk Session",
        "SOURCE STALE nghĩa là gì?",
        "Restart hay Rebuild?",
        "READY_FOR_MAIN nghĩa là gì?",
        "READY_FOR_MAIN không có nghĩa là đã merge main",
        "Sau khi task merge thành công",
        "Test ESP / Kiosk",
        "Stop khác Cleanup thế nào?",
    ]:
        assert phrase in text, f"missing operator-facing phrase: {phrase!r}"
    # internal class names must never leak into user-facing copy
    for internal in ["SandboxRuntimeService", "PortAllocatorService", "SandboxManager", "GitWorkspaceService"]:
        assert internal not in text


def test_help_honest_about_unimplemented_capabilities(client):
    """Section 40: never instruct the user as if an unbuilt button/flow
    exists -- GitHub PR/merge automation and ESP flashing are manual."""
    text = client.get("/help").text
    assert "không bypass GitHub CI" in text
    assert "không tự merge main" in text
    assert "chưa tự động tạo PR hay tự merge" in text
    assert "chưa tự động flash firmware" in text


def test_help_launcher_permission_modes_documented(client):
    text = client.get("/help").text
    assert 'id="open-agent"' in text
    assert "codex --yolo" in text
    assert "claude --dangerously-skip-permissions" in text


def test_contextual_help_links_from_primary_screens(client, git_repo):
    """Section 36: New Task, New Agent Workspace, Sandbox selector, Task
    Detail, Integration, Sandboxes, Repository all link into /help."""
    root, repo = git_repo
    register(client, repo)
    rid = client.get("/api/repositories").json()[0]["id"]

    assert "/help#tasks" in client.get("/tasks").text
    assert "/help#agent-workspaces" in client.get("/workspaces").text
    assert "/help#integration" in client.get("/integrations").text
    assert "/help#global-sandbox" in client.get("/sandboxes").text
    assert "/help#repositories" in client.get("/repositories").text

    client.post("/api/tasks", data={"title": "Contextual link check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")  # BACKLOG's minimal view has none of these; select first
    task_page = client.get(f"/tasks/{tid}").text
    assert "/help#task-detail-sandbox" in task_page
    assert "/help#sandbox-modes" in task_page  # sandbox profile selector
    assert "/help#wizard" in task_page  # wizard stepper's own contextual help link


def test_help_answers_the_manual_verification_questions(client):
    """Section 44: fifteen questions an operator must be able to answer
    from the page alone. Each maps to text that must be present."""
    text = client.get("/help").text
    answers = {
        "Có bug mới thì bắt đầu ở đâu?": "New Task",
        "Làm sao mở Claude đúng source?": "Open Claude",
        "Sandbox dùng để làm gì?": "Sandbox dùng để làm gì?",
        "Xem sandbox chạy port nào ở đâu?": "Xem các môi trường đang chạy",
        "Task Detail có sandbox URL không?": "Xem môi trường của riêng một task",
        "Agent xong thì làm gì?": "Mark Ready",
        "Khi nào Create Integration?": "Create Integration dùng khi nào?",
        "Integration Agent làm gì?": "Integration Agent làm gì?",
        "Backend chưa merge thì ESP test bằng cách nào?": "chưa merge main",
        "SOURCE STALE nghĩa là gì?": "SOURCE STALE nghĩa là gì?",
        "Restart khác Rebuild thế nào?": "Restart hay Rebuild?",
        "READY_FOR_MAIN nghĩa là gì?": "READY_FOR_MAIN nghĩa là gì?",
        "Merge xong sandbox đi đâu?": "Sau khi task merge thành công",
        "Stop khác Cleanup thế nào?": "Stop khác Cleanup thế nào?",
        "Repo mới không hiện thì làm gì?": "tự làm mới mỗi khi bạn mở lại trang",
    }
    missing = [q for q, needle in answers.items() if needle not in text]
    assert not missing, f"/help cannot answer: {missing}"


def test_help_repositories_rescan_wording_is_honest(client):
    """Repositories has no dedicated "Rescan" button -- the page just
    re-scans on every load. Help must describe the real behavior."""
    text = client.get("/help").text
    assert "tự làm mới mỗi khi bạn mở lại trang" in text
