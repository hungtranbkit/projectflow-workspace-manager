"""Sidebar Advanced navigation layout regression: the submenu must
render as a real vertical list (never a horizontal grid row), the
Advanced section must auto-expand and highlight its active child on a
matching route, and there must be no duplicate nav entries anywhere in
the page. Covers the exact bug reported live: at the sidebar's mobile
breakpoint, the old `nav{grid-template-columns:repeat(3,1fr)}` rule
turned the whole sidebar (Advanced's expanded submenu included) into a
multi-column grid, scattering Repositories/Settings/Hướng dẫn beside
and around the submenu instead of below it."""
from __future__ import annotations
import re


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


ADVANCED_HREFS = ["/workspaces", "/sandboxes", "/integrations", "/test-runs"]
TOP_LEVEL_HREFS = ["/", "/tasks", "/changes", "/kanban", "/agents/live", "/repositories", "/settings", "/help"]


def test_advanced_submenu_has_four_distinct_links_with_correct_hrefs(client, git_repo):
    html = client.get("/tasks").text
    m = re.search(r'<div class="nav-submenu">(.*?)</div>', html, re.S)
    assert m, "nav-submenu container not found"
    submenu = m.group(1)
    hrefs = re.findall(r'href="([^"]+)"', submenu)
    assert hrefs == ADVANCED_HREFS


def test_advanced_submenu_items_are_block_level_not_a_horizontal_row(client, git_repo):
    """The regression's real cause: a submenu link with no `display:block`
    (or a grid/flex row ancestor) renders inline, beside its siblings."""
    css = client.get("/static/style.css").text
    assert ".nav-submenu{display:flex;flex-direction:column" in css.replace(" ", "")
    assert ".nav-subitem{display:block" in css.replace(" ", "")
    # the specific bug: the old mobile-only multi-column override must be gone
    assert "grid-template-columns:repeat(3,1fr)" not in css.replace(" ", "")
    assert "nav{grid-template-columns:repeat(2,1fr)}" not in css.replace(" ", "")


def test_no_duplicate_nav_items_anywhere_on_the_page(client, git_repo):
    html = client.get("/tasks").text
    nav_section = re.search(r"<nav class=\"sidebar-nav\">(.*?)</nav>", html, re.S).group(1)
    hrefs = re.findall(r'href="([^"]+)"', nav_section)
    assert len(hrefs) == len(set(hrefs)), f"duplicate nav hrefs: {hrefs}"
    assert set(hrefs) == set(TOP_LEVEL_HREFS) | set(ADVANCED_HREFS)


def test_advanced_expands_and_highlights_active_child_on_matching_route(client, git_repo):
    for path, active_href in [("/workspaces", "/workspaces"), ("/sandboxes", "/sandboxes"),
                               ("/integrations", "/integrations"), ("/test-runs", "/test-runs")]:
        html = client.get(path).text
        assert '<details class="nav-section nav-advanced" open>' in html, f"{path}: Advanced did not auto-expand"
        assert f'class="nav-subitem active" href="{active_href}"' in html, f"{path}: active child not highlighted"


def test_advanced_collapsed_by_default_on_a_non_advanced_route(client, git_repo):
    html = client.get("/tasks").text
    assert '<details class="nav-section nav-advanced">' in html
    assert '<details class="nav-section nav-advanced" open>' not in html


def test_top_level_active_item_highlighted(client, git_repo):
    html = client.get("/kanban").text
    assert 'class="nav-item active" href="/kanban"' in html
    assert 'class="nav-item active" href="/tasks"' not in html


def test_sidebar_renders_on_task_detail_workspace_and_sandbox_pages_without_error(client, git_repo, sandboxable_repo_factory):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Sidebar smoke", "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    for path in [f"/tasks/{tid}", "/workspaces", "/sandboxes", "/integrations", "/test-runs"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert '<nav class="sidebar-nav">' in resp.text, path
