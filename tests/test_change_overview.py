"""Change Overview (first UI surface for the E1-E7 engineering domain).
build_change_overview() is a pure view-model builder over already-real
service state -- see app/services/change_overview.py's own module
docstring -- so most tests here call it directly with hand-built inputs
(fast, deterministic, no HTTP/DB needed) and a smaller HTTP-level set
confirms the routes actually wire real service calls into it end to
end."""
from __future__ import annotations
import json

import pytest

from app.services.change_overview import build_change_overview


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_change(client, title, description="", project_id=None):
    data = {"title": title, "description": description}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post("/api/changes", data=data).json()["id"]


def create_workflow(client, cid, profile="AGENTIC_STANDARD"):
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": profile})
    assert r.status_code == 200, r.text
    return r.json()


def base_kwargs(**overrides):
    kwargs = dict(
        change={"id": 1, "title": "X"}, work_products=[], workflow_state=None,
        architecture_status={"architecture_analysis": None, "architecture_ready": False},
        design_status={"technical_design": None, "ui_ux_design": None, "ui_ux_applicability": {"applicable": False}, "design_ready": False},
        test_design_status={"test_case_set": None, "test_design_ready": False},
        spec_proposals=[], human_decisions_pending=[], agents_completed=0, agents_running=0,
        spec_drift={"stale": False, "reason": None},
    )
    kwargs.update(overrides)
    return kwargs


def section(overview, title):
    return next(s for s in overview["sections"] if s["title"] == title)


def state_of(overview, section_title, item_label):
    return next(i["state"] for i in section(overview, section_title)["checks"] if i["label"] == item_label)


# ================================================================ build_change_overview() unit tests

def test_overview_all_future_when_nothing_started():
    ov = build_change_overview(**base_kwargs())
    assert ov["status"] == "PENDING"
    assert state_of(ov, "Spec", "Requirement Analysis") == "future"
    assert state_of(ov, "Implementation", "Implementation") == "future"


def test_overview_spec_progression():
    wps = [{"kind": "REQUIREMENT_ANALYSIS", "status": "DRAFT"}, {"kind": "FEATURE_SPEC", "status": "APPROVED"}]
    ov = build_change_overview(**base_kwargs(work_products=wps, spec_proposals=[{"review_result": json.dumps({"verdict": "PASS"})}]))
    assert state_of(ov, "Spec", "Requirement Analysis") == "done"
    assert state_of(ov, "Spec", "Feature Spec") == "done"
    assert state_of(ov, "Spec", "Spec Review") == "done"
    assert state_of(ov, "Spec", "Approved") == "done"


def test_overview_ui_ux_skipped_when_not_applicable():
    ov = build_change_overview(**base_kwargs(design_status={
        "technical_design": {"status": "APPROVED"}, "ui_ux_design": None,
        "ui_ux_applicability": {"applicable": False}, "design_ready": False}))
    item = next(i for i in section(ov, "Design")["checks"] if i["label"] == "UI/UX Design")
    assert item["state"] == "skipped"
    assert item["note"] == "Not applicable"


def test_overview_ui_ux_required_when_applicable_and_missing():
    ov = build_change_overview(**base_kwargs(design_status={
        "technical_design": {"status": "APPROVED"}, "ui_ux_design": None,
        "ui_ux_applicability": {"applicable": True}, "design_ready": False}))
    # Technical Design already exists, so UI/UX Design is actionable now
    # (current), not merely "future" -- both design tracks start once
    # SOME design context exists, never gated on each other.
    assert state_of(ov, "Design", "UI/UX Design") == "current"


def test_overview_adr_skipped_for_no_architecture_change():
    wps = [{"kind": "ARCHITECTURE_ANALYSIS", "status": "APPROVED", "content_metadata": json.dumps({"classification": "NO_ARCHITECTURE_CHANGE"})}]
    ov = build_change_overview(**base_kwargs(work_products=wps, architecture_status={"architecture_analysis": wps[0], "architecture_ready": True}))
    assert state_of(ov, "Architecture", "ADR") == "skipped"


def test_overview_test_design_not_started_vs_ready():
    ov1 = build_change_overview(**base_kwargs())
    item1 = next(i for i in section(ov1, "Test Design")["checks"])
    assert item1["state"] == "future" and item1["note"] == "Not started"

    ov2 = build_change_overview(**base_kwargs(test_design_status={"test_case_set": {"id": 1}, "test_design_ready": True}))
    item2 = next(i for i in section(ov2, "Test Design")["checks"])
    assert item2["state"] == "done"


def test_overview_implementation_progress():
    change = {"id": 1, "title": "X", "_tasks": [{"status": "DONE"}, {"status": "ACTIVE"}]}
    ov = build_change_overview(**base_kwargs(change=change))
    item = next(i for i in section(ov, "Implementation")["checks"])
    assert item["state"] == "current"
    assert "1/2" in item["note"]


def test_overview_implementation_done_when_all_tasks_done():
    change = {"id": 1, "title": "X", "_tasks": [{"status": "DONE"}]}
    ov = build_change_overview(**base_kwargs(change=change))
    assert state_of(ov, "Implementation", "Implementation") == "done"


def test_overview_state_class_and_headline_mapping():
    ov = build_change_overview(**base_kwargs(workflow_state={"status": "WAITING_HUMAN", "current_stage": "REVIEW"}))
    assert ov["state_class"] == "action_required"
    assert "human decision" in ov["headline"].lower()
    ov2 = build_change_overview(**base_kwargs(workflow_state={"status": "COMPLETE", "current_stage": None}))
    assert ov2["state_class"] == "complete"


def test_overview_human_decisions_and_agents_passthrough():
    hd = [{"id": 1, "question": "Q?", "reason": "R", "subject_type": "work_product", "spec_change_signal": "OTHER"}]
    ov = build_change_overview(**base_kwargs(human_decisions_pending=hd, agents_completed=2, agents_running=1,
                                              spec_drift={"stale": True, "reason": "PLAN_SPEC_DRIFT"}))
    assert ov["human_decisions_pending"] == hd
    assert ov["agents_completed"] == 2 and ov["agents_running"] == 1
    assert ov["spec_drift"]["reason"] == "PLAN_SPEC_DRIFT"


# ================================================================ HTTP routes -- real services wired in

def test_changes_list_empty(client):
    r = client.get("/changes")
    assert r.status_code == 200
    assert "No Changes yet" in r.text


def test_changes_list_shows_row(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Offline Kiosk", project_id=rid)
    r = client.get("/changes")
    assert r.status_code == 200
    assert "Offline Kiosk" in r.text
    assert f'/changes/{cid}' in r.text


def test_change_detail_404_for_missing_change(client):
    r = client.get("/changes/999999")
    assert r.status_code == 404


def test_change_detail_bare_change_no_workflow(client):
    cid = new_change(client, "Bare change")
    r = client.get(f"/changes/{cid}")
    assert r.status_code == 200
    assert "Bare change" in r.text
    assert "Not started" in r.text or "Waiting" in r.text


def test_change_detail_reflects_real_workflow_state(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Workflow-backed change", project_id=rid)
    create_workflow(client, cid, "CONTROLLED")
    r = client.get(f"/changes/{cid}")
    assert r.status_code == 200
    assert "CONTROLLED" in r.text


def test_change_detail_shows_pending_human_decision_and_links_to_decisions_tab(client):
    """E7.5.3 gave Decisions its own dedicated tab -- the Overview page
    surfaces the blocker and links there; the actual resolve form lives
    on /changes/{cid}/decisions (see test_engineering_lifecycle_ui.py)."""
    cid = new_change(client, "Decision change")
    client.app.state.human_decisions.create("change", cid, "Cascade delete?", "undefined business rule", "OTHER")
    r = client.get(f"/changes/{cid}")
    assert r.status_code == 200
    assert "Cascade delete?" in r.text
    assert "1 human decision" in r.text
    assert f'/changes/{cid}/decisions' in r.text


def test_change_detail_resolve_route_redirects_and_resolves(client):
    cid = new_change(client, "Resolve change")
    hd_id = client.app.state.human_decisions.create("change", cid, "Which option?", "tradeoff", "OTHER")
    r = client.post(f"/changes/{cid}/human-decisions/{hd_id}/resolve", data={"resolution_note": "Option A."}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/changes/{cid}/decisions#pending"
    resolved = client.app.state.human_decisions.get(hd_id)
    assert resolved["resolved"] == 1
    assert resolved["resolution_note"] == "Option A."
    r2 = client.get(f"/changes/{cid}")
    assert "Which option?" not in r2.text


def test_change_detail_shows_agent_counts(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Agents change", project_id=rid)
    r = client.post("/api/tasks", data={"title": "Task for agents", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Task for agents"][0]
    client.app.state.changes.attach_task_to_change(cid, tid)
    wsid = client.app.state.db.execute(
        "INSERT INTO agent_workspaces(task_id,repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,status) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (tid, rid, "claude", "Backend", "task/agents-x", "/tmp/agents-x", "main", "abc", "READY"))
    client.app.state.db.execute(
        "INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status) VALUES(?,?,?,?,?,?)",
        (tid, wsid, "claude", "default", "/tmp/agents-x", "EXITED"))
    client.app.state.db.execute(
        "INSERT INTO agent_sessions(task_id,workspace_id,agent,command_profile,cwd,status) VALUES(?,?,?,?,?,?)",
        (tid, wsid, "claude", "default", "/tmp/agents-x", "RUNNING"))
    r = client.get(f"/changes/{cid}")
    assert r.status_code == 200
    assert "Agents Completed" in r.text and "Agents Running" in r.text


def test_nav_link_present_on_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/changes"' in r.text
