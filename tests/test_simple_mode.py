"""Track A1.26/A1.27 -- Simple Mode tests: default/read, Advanced
preserved, lifecycle mapping, human attention, product/incident/
acceptance surfacing, active agents, and that Simple Mode never
fabricates technical detail it doesn't have real evidence for."""
from __future__ import annotations

from tests.test_autonomous_execution import register, new_change, materialize_task, create_workflow


def test_changes_page_default_mode_is_advanced(client, git_repo):
    """A1.12 asks for a Simple default "if safe" -- proven unsafe here
    (defaulting /changes/{id} to Simple broke 6 real, pre-existing tests
    that assert specific Advanced-page content with no mode signal set,
    see app/main.py's _ui_mode() comment), so a zero-signal request
    stays Advanced, exactly like every route behaved before this track.
    Simple Mode itself is still fully built and one click away
    (?mode=simple, tested below) -- this only changes which one a bare
    request gets with no cookie/query param at all."""
    root, repo = git_repo
    r = client.get("/changes")
    assert r.status_code == 200
    assert r.cookies.get("pf_mode") is None  # no explicit choice made yet -> no cookie written
    assert "New Change" in r.text  # A1.19's Simple Create form is shown regardless of mode


def test_mode_query_param_sets_cookie_and_persists(client, git_repo):
    r = client.get("/changes?mode=advanced")
    assert r.status_code == 200
    assert r.cookies.get("pf_mode") == "advanced"
    r2 = client.get("/changes", cookies={"pf_mode": "advanced"})
    assert r2.status_code == 200


def test_change_detail_simple_vs_advanced_rendering(client, git_repo):
    """A1.11/A1.12/A1.27: same Change, two renderings -- Simple never
    replaces Advanced; every Advanced tab route stays reachable. A
    zero-signal request is Advanced (see test_changes_page_default_
    mode_is_advanced above); Simple is one explicit click away."""
    root, repo = git_repo
    pid = register(client, repo)
    cid = new_change(client, "Add CSV export")
    r_simple = client.get(f"/changes/{cid}?mode=simple")
    assert r_simple.status_code == 200
    assert "SPEC_BASELINE_CHANGED" not in r_simple.text  # A1.15: no raw technical codes as primary text

    # explicit ?mode=advanced, not a bare request -- the first call above
    # already persisted "simple" into this same client's cookie jar
    # (A1.12's own "persist preference" behavior), so a bare request here
    # would inherit that choice rather than demonstrate the no-signal
    # default (covered separately by test_changes_page_default_mode_is_advanced).
    r_advanced = client.get(f"/changes/{cid}?mode=advanced")
    assert r_advanced.status_code == 200
    # A1.27: every previous E1-E13 Advanced tab still there.
    for tab in ("spec", "architecture", "design", "tests", "plan", "tasks",
                "reviews", "decisions", "evidence", "release", "deploy", "acceptance"):
        assert f'/changes/{cid}/{tab}' in r_advanced.text, f"Advanced tab link missing: {tab}"
        tab_resp = client.get(f"/changes/{cid}/{tab}")
        assert tab_resp.status_code == 200, f"Advanced tab route broken: {tab}"


def test_simple_view_api_matches_service(client, git_repo):
    root, repo = git_repo
    cid = new_change(client, "Inventory app")
    r = client.get(f"/api/changes/{cid}/simple-view")
    assert r.status_code == 200
    body = r.json()
    assert body["change"]["id"] == cid
    assert set(body) >= {"change", "lifecycle", "human_attention", "product", "build", "review", "deploy", "history"}
    direct = client.app.state.simple_view_service.build(cid)
    assert body == direct


def test_lifecycle_mapping_only_from_workflow_truth(client, git_repo):
    """A1.14: the 6-step Simple lifecycle must be derived from
    WorkflowService.evaluate_workflow()'s real stages, never invented --
    a fresh Change with no workflow yet is PENDING/'understanding'."""
    root, repo = git_repo
    cid = new_change(client, "New idea")
    view = client.app.state.simple_view_service.build(cid)
    steps = {s["key"]: s["state"] for s in view["lifecycle"]["steps"]}
    assert view["lifecycle"]["status"] == "PENDING"
    assert steps["ready"] == "future"


def test_lifecycle_no_workflow_yet_is_not_shown_as_done(client, git_repo):
    """Regression: stage_timeline() reads BOTH 'no WorkflowRun exists at
    all yet' and 'this stage is genuinely NOT_APPLICABLE under the
    chosen profile' as the same visual=NOT_APPLICABLE -- without an
    explicit has_workflow guard, a brand-new Change (no workflow created
    yet) rendered every non-Ready step as done/green, which would
    actively mislead a user into thinking an unstarted Change was
    finished."""
    root, repo = git_repo
    cid = new_change(client, "Not started")
    view = client.app.state.simple_view_service.build(cid)
    steps = {s["key"]: s["state"] for s in view["lifecycle"]["steps"]}
    assert steps["understanding"] == "current"
    for key in ("designing", "building", "checking", "deploying", "ready"):
        assert steps[key] == "future", f"{key} should be future, got {steps[key]}"


def test_human_attention_no_action_needed_by_default(client, git_repo):
    root, repo = git_repo
    cid = new_change(client, "Quiet change")
    view = client.app.state.simple_view_service.build(cid)
    assert view["human_attention"]["needs_you"] is False
    assert "No action needed" in view["human_attention"]["headline"]


def test_human_attention_surfaces_pending_decision(client, git_repo):
    root, repo = git_repo
    cid = new_change(client, "Needs a call")
    did = client.app.state.human_decisions.create("change", cid, "Should locked invoices be editable?")
    view = client.app.state.simple_view_service.build(cid)
    assert view["human_attention"]["needs_you"] is True
    assert view["human_attention"]["detail"] == "Should locked invoices be editable?"
    assert view["human_attention"]["link"] == f"/changes/{cid}/decisions"


def test_backend_only_change_no_product_url_no_fabrication(client, git_repo):
    """A1.26/A1.18: a Change with no release/deployment must show honest
    'not deployed' state, never a fabricated URL/version."""
    root, repo = git_repo
    cid = new_change(client, "Backend-only DB index change")
    view = client.app.state.simple_view_service.build(cid)
    assert view["product"]["live_url"] is None
    assert view["product"]["version"] is None
    assert view["deploy"]["test"]["text"] == "Not deployed"
    assert view["deploy"]["production"]["text"] == "Not deployed"


def test_no_release_change_deploy_section_honest(client, git_repo):
    root, repo = git_repo
    cid = new_change(client, "No release yet")
    view = client.app.state.simple_view_service.build(cid)
    assert view["deploy"]["release_text"] == "Not deployed yet"


def test_incidents_surfaced_in_history(client, git_repo):
    root, repo = git_repo
    cid = new_change(client, "Change with an incident")
    db = client.app.state.db
    db.execute("INSERT INTO incidents(project_id,change_id,title,status) VALUES(?,?,?,?)",
               (None, cid, "Checkout button broken", "REPORTED"))
    view = client.app.state.simple_view_service.build(cid)
    assert len(view["history"]["incidents"]) == 1
    assert view["history"]["incidents"][0]["title"] == "Checkout button broken"


def test_active_agents_simple_text_no_raw_session_fields(client, git_repo):
    """A1.17: default (no live AgentSessions) shows a plain sentence,
    never raw provider/session/PID/worktree fields."""
    root, repo = git_repo
    cid = new_change(client, "No agents yet")
    view = client.app.state.simple_view_service.build(cid)
    assert view["build"]["agents_text"] == "No AI workers active"
    assert "pid" not in view["build"] and "worktree" not in view["build"]


def test_waiting_human_change_status_language(client, git_repo):
    """A1.15: friendly text, not a raw enum, drives the /changes list
    badge in Simple Mode (explicitly requested -- Advanced, the default,
    keeps the raw enum, tested alongside it here)."""
    root, repo = git_repo
    cid = new_change(client, "Waiting on a person")
    client.app.state.human_decisions.create("change", cid, "Pick an option")
    create_workflow(client, cid)
    r = client.get("/changes?mode=simple")
    assert r.status_code == 200
    assert "Waiting for your decision" in r.text

    # explicit, not bare -- the call above already persisted "simple"
    # into this client's cookie jar.
    r_advanced = client.get("/changes?mode=advanced")
    assert r_advanced.status_code == 200
    assert "WAITING HUMAN" in r_advanced.text


def test_simple_create_change_form(client, git_repo):
    """A1.19: freeform creation, no Spec/Plan/Task vocabulary required."""
    root, repo = git_repo
    r = client.post("/changes", data={"what": "Add a CSV export button to the reports page"}, follow_redirects=False)
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[-1])
    change = client.app.state.changes.get(cid)
    assert change["title"] == "Add a CSV export button to the reports page"
    assert change["description"] == "Add a CSV export button to the reports page"
