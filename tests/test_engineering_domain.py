"""Engineering Domain Foundation (Phase E1): Change and WorkProduct as
first-class entities ABOVE the existing Task model. Every test here
either exercises the new domain directly (app.state.changes/
work_products/trace, the real ChangeService/WorkProductService/
TraceService instances the running app wires up) or proves the
existing Task/Supervisor/SpecGate/TaskDecisionService paths are
completely unaffected by a Task having (or not having) a change_id."""
from __future__ import annotations
import json

import pytest

from app.services.change_service import ChangeError
from app.services.work_product_service import WorkProductError


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_task(client, title, risk="LOW"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def add_workspace(client, tid, rid, agent="claude", role="Backend"):
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": agent, "role": role, "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent and w["role"] == role][-1]


# ===================================================================== Change

def test_change_create_and_retrieve_via_service(client):
    svc = client.app.state.changes
    cid = svc.create(title="Kiosk session overhaul", description="Rework session handling", change_type="FEATURE", risk_level="HIGH")
    row = svc.get(cid)
    assert row["title"] == "Kiosk session overhaul"
    assert row["description"] == "Rework session handling"
    assert row["change_type"] == "FEATURE"
    assert row["risk_level"] == "HIGH"
    assert row["lifecycle_state"] == "NEW"
    assert row["closed_at"] is None


def test_change_requires_a_title(client):
    with pytest.raises(ChangeError):
        client.app.state.changes.create(title="  ")


def test_change_rejects_unknown_type_and_risk(client):
    svc = client.app.state.changes
    with pytest.raises(ChangeError):
        svc.create(title="x", change_type="NOT_A_TYPE")
    with pytest.raises(ChangeError):
        svc.create(title="x", risk_level="EXTREME")


def test_change_lifecycle_transitions_and_terminal_closed_at(client):
    svc = client.app.state.changes
    cid = svc.create(title="Lifecycle test")
    svc.set_lifecycle_state(cid, "ANALYZING")
    row = svc.get(cid)
    assert row["lifecycle_state"] == "ANALYZING"
    assert row["closed_at"] is None

    svc.set_lifecycle_state(cid, "DONE")
    row = svc.get(cid)
    assert row["lifecycle_state"] == "DONE"
    assert row["closed_at"] is not None


def test_change_lifecycle_rejects_unknown_state(client):
    svc = client.app.state.changes
    cid = svc.create(title="x")
    with pytest.raises(ChangeError):
        svc.set_lifecycle_state(cid, "TELEPORTED")


def test_change_project_scoping_via_repositories(client, git_repo):
    """E1.6: project identity reuses repositories(id) -- no separate
    projects table. list(project_id=...) filters correctly."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    svc = client.app.state.changes
    scoped = svc.create(title="Scoped change", project_id=rid)
    unscoped = svc.create(title="Unscoped change")
    assert svc.get(scoped)["project_id"] == rid
    assert svc.get(unscoped)["project_id"] is None
    listed = svc.list(project_id=rid)
    ids = {c["id"] for c in listed}
    assert scoped in ids and unscoped not in ids


def test_change_create_rejects_unknown_project_id(client):
    with pytest.raises(ChangeError):
        client.app.state.changes.create(title="x", project_id=999999)


# ============================================================ Change <-> Task

def test_attach_task_to_change_and_list_tasks_for_change(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    tid = new_task(client, "Task under a change")
    svc = client.app.state.changes
    cid = svc.create(title="Parent change", project_id=rid)

    svc.attach_task_to_change(cid, tid)
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["change_id"] == cid

    tasks = svc.list_tasks_for_change(cid)
    assert [x["id"] for x in tasks] == [tid]


def test_attach_task_to_change_rejects_unknown_ids(client, git_repo):
    root, repo = git_repo
    svc = client.app.state.changes
    tid = new_task(client, "Real task")
    with pytest.raises(ChangeError):
        svc.attach_task_to_change(999999, tid)
    cid = svc.create(title="Real change")
    with pytest.raises(ChangeError):
        svc.attach_task_to_change(cid, 999999)


def test_legacy_task_with_no_change_is_completely_unaffected(client, git_repo):
    """Backward compatibility (E1.2/E1.8): a Task that never touches
    Change at all keeps change_id NULL and behaves exactly as before."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    tid = new_task(client, "Never touches Change")
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["change_id"] is None

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "BACKLOG"

    svc = client.app.state.changes
    assert svc.list_tasks_for_change(1) == []  # no change 1 exists yet in this test db; still a clean empty list, no crash


def test_change_api_http_surface(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    tid = new_task(client, "HTTP surface task")

    r = client.post("/api/changes", data={"title": "Via HTTP", "change_type": "BUG", "risk_level": "LOW", "project_id": str(rid)})
    assert r.status_code == 200, r.text
    change = r.json()
    cid = change["id"]
    assert change["change_type"] == "BUG"

    assert client.get(f"/api/changes/{cid}").json()["id"] == cid
    assert any(c["id"] == cid for c in client.get("/api/changes").json())
    assert any(c["id"] == cid for c in client.get(f"/api/changes?project_id={rid}").json())

    r = client.post(f"/api/changes/{cid}/tasks/{tid}/attach")
    assert r.status_code == 200
    assert r.json()["change_id"] == cid

    tasks = client.get(f"/api/changes/{cid}/tasks").json()
    assert [t["id"] for t in tasks] == [tid]

    r = client.post(f"/api/changes/{cid}/lifecycle", data={"state": "PLANNING"})
    assert r.status_code == 200
    assert r.json()["lifecycle_state"] == "PLANNING"


def test_change_api_rejects_bad_input_with_json_400(client):
    r = client.post("/api/changes", data={"title": "   "})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False and "message" in body

    r2 = client.post("/api/changes", data={"title": "ok", "change_type": "NONSENSE"})
    assert r2.status_code == 400


def test_change_get_404_for_unknown_id(client):
    r = client.get("/api/changes/999999")
    assert r.status_code == 404


# =================================================================== WorkProduct

def test_work_product_create_and_retrieve(client):
    svc = client.app.state.work_products
    wpid = svc.create(kind="ADR", title="Use SQLite for local state", content_ref="specs/adr/0001.md", content_metadata={"decided_by": "team"})
    row = svc.get(wpid)
    assert row["kind"] == "ADR"
    assert row["title"] == "Use SQLite for local state"
    assert row["status"] == "DRAFT"
    assert row["content_ref"] == "specs/adr/0001.md"
    assert json.loads(row["content_metadata"]) == {"decided_by": "team"}


def test_work_product_rejects_unknown_kind_and_status(client):
    svc = client.app.state.work_products
    with pytest.raises(WorkProductError):
        svc.create(kind="NOT_A_KIND", title="x")
    with pytest.raises(WorkProductError):
        svc.create(kind="ADR", title="x", status="NOT_A_STATUS")


def test_work_product_attach_to_change(client):
    changes = client.app.state.changes
    wps = client.app.state.work_products
    cid = changes.create(title="Change with specs")
    wpid = wps.create(kind="FEATURE_SPEC", title="Spec for the change", change_id=cid)
    listed = wps.list_for_change(cid)
    assert [w["id"] for w in listed] == [wpid]


def test_work_product_attach_to_task(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Task producing a design")
    wps = client.app.state.work_products
    wpid = wps.create(kind="TECHNICAL_DESIGN", title="Design doc", task_id=tid)
    listed = wps.list_for_task(tid)
    assert [w["id"] for w in listed] == [wpid]


def test_work_product_input_output_relationships(client, git_repo):
    """E1.4: Task -> input/output WorkProduct references, never large
    documents stored on the Task row itself."""
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Implementation task")
    wps = client.app.state.work_products

    spec_wp = wps.create(kind="FEATURE_SPEC", title="The spec this task implements")
    design_wp = wps.create(kind="TECHNICAL_DESIGN", title="The design this task implements")
    code_wp = wps.create(kind="CODE_CHANGE", title="The code this task produces", task_id=tid)

    wps.link_task(tid, spec_wp, "INPUT")
    wps.link_task(tid, design_wp, "INPUT")
    wps.link_task(tid, code_wp, "OUTPUT")

    assert {w["id"] for w in wps.inputs_for_task(tid)} == {spec_wp, design_wp}
    assert {w["id"] for w in wps.outputs_for_task(tid)} == {code_wp}


def test_work_product_link_task_rejects_unknown_direction(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "x")
    wps = client.app.state.work_products
    wpid = wps.create(kind="ADR", title="x")
    with pytest.raises(WorkProductError):
        wps.link_task(tid, wpid, "SIDEWAYS")


def test_work_product_link_is_idempotent(client, git_repo):
    """Linking the same (task, work_product, direction) twice never
    duplicates the relationship (UNIQUE constraint + ON CONFLICT DO NOTHING)."""
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "x")
    wps = client.app.state.work_products
    wpid = wps.create(kind="ADR", title="x")
    wps.link_task(tid, wpid, "INPUT")
    wps.link_task(tid, wpid, "INPUT")
    assert len(wps.inputs_for_task(tid)) == 1


def test_work_product_supersede_marks_old_row_never_deletes_it(client):
    """History-friendly (E1.3): a revision is a new row; the old one is
    marked SUPERSEDED, its content/title untouched."""
    wps = client.app.state.work_products
    v1 = wps.create(kind="ADR", title="Original decision", content_ref="v1")
    v2 = wps.create(kind="ADR", title="Revised decision", content_ref="v2", supersedes_id=v1)

    old = wps.get(v1)
    new = wps.get(v2)
    assert old["status"] == "SUPERSEDED"
    assert old["title"] == "Original decision"  # never rewritten
    assert old["content_ref"] == "v1"
    assert new["status"] == "DRAFT"
    assert new["supersedes_id"] == v1


def test_work_product_api_http_surface(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "HTTP work product task")

    r = client.post("/api/work-products", data={
        "kind": "VERIFICATION_REPORT", "title": "Verified via HTTP", "task_id": str(tid),
        "content_metadata": json.dumps({"suite": "pytest"}),
    })
    assert r.status_code == 200, r.text
    wp = r.json()
    wpid = wp["id"]
    assert wp["kind"] == "VERIFICATION_REPORT"

    assert client.get(f"/api/work-products/{wpid}").json()["id"] == wpid

    r = client.post(f"/api/tasks/{tid}/work-products/{wpid}/link", data={"direction": "OUTPUT"})
    assert r.status_code == 200
    body = client.get(f"/api/tasks/{tid}/work-products").json()
    assert [w["id"] for w in body["outputs"]] == [wpid]
    assert body["inputs"] == []


def test_work_product_api_rejects_bad_json_metadata(client):
    r = client.post("/api/work-products", data={"kind": "ADR", "title": "x", "content_metadata": "{not json"})
    assert r.status_code == 400


def test_work_product_get_404_for_unknown_id(client):
    assert client.get("/api/work-products/999999").status_code == 404


# ======================================================================= Trace

def test_trace_link_and_lookup_both_directions(client):
    trace = client.app.state.trace
    trace.link("change", 7, "spec_feature", "FEAT-SPEC-LAYER", relation="IMPLEMENTS")
    trace.link("work_product", 3, "deployment", 42, relation="RELEASED_AS")

    from_change = trace.for_source("change", 7)
    assert len(from_change) == 1
    assert from_change[0]["target_type"] == "spec_feature"
    assert from_change[0]["target_id"] == "FEAT-SPEC-LAYER"
    assert from_change[0]["relation"] == "IMPLEMENTS"

    to_deployment = trace.for_target("deployment", 42)
    assert len(to_deployment) == 1
    assert to_deployment[0]["source_type"] == "work_product"


def test_trace_link_is_idempotent(client):
    trace = client.app.state.trace
    trace.link("change", 1, "spec_feature", "X")
    trace.link("change", 1, "spec_feature", "X")
    assert len(trace.for_source("change", 1)) == 1


# ============================================================ Backward compat

def test_spec_gate_unaffected_by_change_linkage(client, git_repo):
    """E1.8: SpecGate must behave identically whether or not a Task is
    attached to a Change -- Change is a completely orthogonal concept
    to spec_change_classification/SpecGate."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    tid = new_task(client, "Change-linked task")
    cid = client.app.state.changes.create(title="Parent change")
    client.app.state.changes.attach_task_to_change(cid, tid)

    gate = client.get(f"/api/tasks/{tid}/spec-gate").json()
    assert gate["outcome"] == "NOT_APPLICABLE"  # exactly the same as an unlinked Task


def test_supervisor_still_starts_a_change_linked_task(client, git_repo):
    """E1.8: _start_builder_session (the real Supervisor entry point)
    must still start an Agent session normally for a Task that has a
    change_id -- Change attachment never blocks or alters Agent start."""
    from app.launchers import AgentLauncher
    root, repo = git_repo
    rid = register(client, repo, "demo")
    client.app.state.agent_sessions.launchers = {"claude": AgentLauncher("Claude", "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1

    tid = new_task(client, "Change-linked, still startable")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    cid = client.app.state.changes.create(title="Parent change")
    client.app.state.changes.attach_task_to_change(cid, tid)

    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=?", (w["id"],))
    assert session["status"] == "RUNNING"


def test_task_decision_service_unaffected_by_change_linkage(client, git_repo):
    """E1.8: TaskDecisionService.evaluate()'s status/stage/checklist
    computation reads nothing about change_id -- identical result for a
    Task with or without one."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    tid = new_task(client, "Compare decision before/after attach")
    client.post(f"/api/tasks/{tid}/select")
    before = client.get(f"/api/tasks/{tid}/decision").json()

    cid = client.app.state.changes.create(title="Parent change")
    client.app.state.changes.attach_task_to_change(cid, tid)
    after = client.get(f"/api/tasks/{tid}/decision").json()

    assert before["status"] == after["status"] == "ACTIVE"
    assert before["stage"] == after["stage"]
    assert before["next_action"]["action"] == after["next_action"]["action"]
