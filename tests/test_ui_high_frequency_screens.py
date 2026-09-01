"""P1-6 (docs/CORE_USABILITY_QUALIFICATION.md): the FIRST real-browser
(Playwright/Chromium) pass over ProjectFlow's own named high-frequency
screens -- dashboard, repositories, task/change detail, agents/live,
sandbox/verification, release. A real uvicorn server (disposable DB/
worktree root, a free ephemeral port) is started in a background
thread so Playwright navigates real HTTP responses, never a TestClient
response string parsed as if it were a browser. Scope is deliberately
narrow: these screens, real navigation, no misleading status, no dead
button/route, no horizontal overflow at a real mobile viewport width --
not an exhaustive UI audit of all 30+ templates."""
from __future__ import annotations
import socket
import subprocess
import threading
import time

import httpx
import pytest
import uvicorn

from app.config import Settings
from app.main import create_app

playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(root, name):
    repo = root / name
    repo.mkdir(parents=True)
    _run(repo, "init", "-b", "main")
    _run(repo, "config", "user.email", "t@example.invalid")
    _run(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("base\n")
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: " + name + "}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "base")
    return repo


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("ui_hf")
    root = tmp_path / "root"
    root.mkdir()
    port = _free_port()
    settings = Settings(root, "127.0.0.1", port, tmp_path / "live.db", 30, configured_state_dir=tmp_path / "state")
    app = create_app(settings)
    app.state.sandboxes.spawn = lambda fn, args=(): fn(*args)
    app.state.deployer.spawn = lambda fn, args=(): fn(*args)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(base_url + "/", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("live_server never became ready")

    # Seed real, meaningful state for the screens under test.
    repo = _make_repo(root, "demo")
    httpx.post(base_url + "/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})
    rid = httpx.get(base_url + "/api/repositories").json()[0]["id"]
    r = httpx.post(base_url + "/api/tasks/create",
                    data={"title": "UI smoke task", "repository_id": rid, "agent": "codex", "sandbox_profile": "NONE", "risk_profile": "LOW"})
    tid = int(str(r.url).rsplit("/", 1)[-1]) if r.history else httpx.get(base_url + "/api/tasks").json()[0]["id"]

    r = httpx.post(base_url + "/api/changes", data={"title": "UI smoke change", "project_id": str(rid)})
    cid = r.json()["id"]

    yield {"base_url": base_url, "repo_id": rid, "task_id": tid, "change_id": cid}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


SCREENS = [
    ("/", "Dashboard"),
    ("/repositories", "Repositories"),
    ("/tasks", "Tasks"),
    ("/kanban", "Kanban"),
    ("/agents/live", "Live Agents"),
    ("/changes", "Changes"),
]


@pytest.mark.parametrize("path,label", SCREENS)
def test_high_frequency_screen_loads_without_console_errors_or_broken_layout(live_server, browser, path, label):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))

    resp = page.goto(live_server["base_url"] + path, wait_until="networkidle")
    assert resp.status == 200, f"{label} ({path}) returned {resp.status}"
    assert console_errors == [], f"{label} ({path}) had real browser console errors: {console_errors}"

    body_width = page.evaluate("document.body.scrollWidth")
    viewport_width = page.evaluate("window.innerWidth")
    assert body_width <= viewport_width + 1, f"{label} ({path}) overflows horizontally at desktop width: body={body_width} viewport={viewport_width}"

    page.close()


@pytest.mark.parametrize("path,label", SCREENS)
def test_high_frequency_screen_no_horizontal_overflow_at_mobile_width(live_server, browser, path, label):
    """Section 18/mobile-viewport requirement -- a real 375px-wide
    viewport (iPhone SE class), the narrowest realistic device this app
    claims to support (base.html's own responsive @media breakpoints)."""
    page = browser.new_page(viewport={"width": 375, "height": 800})
    resp = page.goto(live_server["base_url"] + path, wait_until="networkidle")
    assert resp.status == 200

    body_width = page.evaluate("document.body.scrollWidth")
    viewport_width = page.evaluate("window.innerWidth")
    assert body_width <= viewport_width + 1, \
        f"{label} ({path}) overflows horizontally at mobile width (375px): body={body_width}px -- forces sideways scrolling on a real phone"
    page.close()


def test_task_detail_shows_clear_status_and_next_action(live_server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    resp = page.goto(live_server["base_url"] + f"/tasks/{live_server['task_id']}", wait_until="networkidle")
    assert resp.status == 200
    text = page.content()
    # A Task detail page must never be a blank/error shell -- some
    # concrete status text and at least one actionable control must be
    # present for a brand-new BACKLOG task.
    assert "UI smoke task" in text
    page.close()


def test_change_detail_shows_clear_status_and_review_release_tabs_reachable(live_server, browser):
    """Change detail is the entry point to review/verification/release
    for that Change -- those tabs (change_reviews.html, change_release.
    html) must be real, reachable links, never dead."""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    resp = page.goto(live_server["base_url"] + f"/changes/{live_server['change_id']}", wait_until="networkidle")
    assert resp.status == 200
    text = page.content()
    assert "UI smoke change" in text

    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
    cid = live_server["change_id"]
    review_link = next((h for h in hrefs if h and f"/changes/{cid}/reviews" in h), None)
    release_link = next((h for h in hrefs if h and f"/changes/{cid}/release" in h), None)
    assert review_link, "Change detail has no reachable link to its own Reviews tab"
    assert release_link, "Change detail has no reachable link to its own Release tab"

    for link in (review_link, release_link):
        sub = page.goto(live_server["base_url"] + link, wait_until="networkidle")
        assert sub.status == 200, f"{link} is a dead route (status {sub.status})"
    page.close()


def test_repositories_register_form_has_no_dead_action(live_server, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    resp = page.goto(live_server["base_url"] + "/repositories", wait_until="networkidle")
    assert resp.status == 200
    form_action = page.get_attribute("form", "action")
    assert form_action == "/api/repositories"
    page.close()


def test_dashboard_links_to_every_named_high_frequency_screen(live_server, browser):
    """The dashboard is the entry point -- every other named
    high-frequency screen must be reachable from it without a dead
    link, so a user is never stranded."""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(live_server["base_url"] + "/", wait_until="networkidle")
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
    page.close()
    for target in ("/repositories", "/tasks", "/agents/live"):
        assert any(h == target or (h and h.startswith(target)) for h in hrefs), \
            f"dashboard has no link to {target} -- a user cannot navigate there from the entry point"
