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
TOP_LEVEL_HREFS = ["/", "/tasks", "/changes", "/kanban", "/incidents", "/agents/live", "/steward", "/repositories", "/settings", "/help"]


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
    payload = {'title': 'Calculator API: return arithmetic results', 'implementation_prompt': '## OBJECTIVE\nFor Calculator API: return arithmetic results, implement a calculator endpoint that returns the exact JSON result for each valid numeric input.\n\n## BASELINE\nThe repository is `app/main.py`, branch integration/orchestrator-v1-20261123 at commit `26c1d3c`. Currently GET /api/calculator returns 404. The Python version is 3.14; assumptions are that FastAPI is already installed.\n\n## SCOPE\nIn scope: change `app/main.py` and add `tests/test_calculator_api.py` for the calculator response. Out of scope: production deployment, authentication changes, and unrelated repositories.\n\n## EXECUTION_PLAN\n1. Inspect `app/main.py` and run `git status --short` before editing.\n2. Reproduce the missing response with `pytest -q tests/test_calculator_api.py`.\n3. Implement the calculator route in `app/main.py` and preserve the existing auth middleware.\n4. Verify the implementation with `pytest -q tests/test_calculator_api.py` and `./scripts/preflight.sh`.\n\n## CONSTRAINTS\nDependencies: existing FastAPI and pytest only. Security boundary: do not expose secrets or weaken authentication. Production must remain untouched. Rollback: revert the scoped `app/main.py` and test changes if the acceptance checks fail.\n\n## ACCEPTANCE_DOD\nAutomated test: run `pytest -q tests/test_calculator_api.py`; expected exit code 0 and GET /api/calculator returns 200 with the exact sum. Verify invalid input returns 422.\n\n## EVIDENCE\nRecord command output, exit status, test report, and resulting commit SHA in `docs/orchestration-standard/ORCH-STD-20261123/EVIDENCE.json`; retain the API result and rollback proof.\n', 'agent': 'claude', 'primary_role': 'Backend', 'primary_base_branch': 'main', 'sandbox_profile': 'NONE', 'risk_profile': 'LOW', 'spec_change_classification': 'NEW_FEATURE', 'spec_feature_id': 'FEAT-ORCHESTRATION-STANDARD', 'spec_version': '1', 'spec_requirement_ids': 'REQ-OS-001', 'spec_acceptance_ids': 'AC-OS-001', 'spec_invariant_ids': 'INV-OS-001'}
    payload["repository_id"] = str(rid)
    r = client.post("/api/tasks/create", data=payload, follow_redirects=False)
    assert r.status_code == 303, r.text
    tid = int(r.headers["location"].split("/")[-1])
    for path in [f"/tasks/{tid}", "/workspaces", "/sandboxes", "/integrations", "/test-runs"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert '<nav class="sidebar-nav">' in resp.text, path
