"""Spec Layer V1 (S5/S6): SpecGate wired into _start_builder_session --
the one real place an Agent session starts (create_session,
_resume_builder_session, start_all_builders, setup_and_start all go
through it). A behavior-changing Task whose spec linkage doesn't PASS
never gets a live Agent; a legacy/unclassified Task is completely
unaffected (REQ-005 backward compatibility). Also covers S6 (spec
context actually appears in the delivered Builder Prompt) and the
`/api/spec/*` + `/api/tasks/{tid}/spec*` HTTP surface (S4)."""
from __future__ import annotations
import json

from app.launchers import AgentLauncher


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def new_task(client, title, risk="LOW"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def add_workspace(client, tid, rid, agent="claude", role="Backend"):
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": agent, "role": role, "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent and w["role"] == role][-1]


def setup_fast_ready_launcher(client, script="echo READY; cat"):
    client.app.state.agent_sessions.launchers = {"claude": AgentLauncher("Claude", "bash", ("-c", script))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1


def set_spec(client, tid, **fields):
    data = {"classification": "", "feature_id": "", "spec_version": "", "requirement_ids": "", "acceptance_ids": "", "invariant_ids": ""}
    data.update(fields)
    r = client.post(f"/api/tasks/{tid}/spec", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


# ---------------------------------------------------------------- API surface

def test_spec_registry_endpoint_reports_the_real_shipped_tree(client, git_repo):
    root, repo = git_repo
    r = client.get("/api/spec/registry")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["baseline_sha256"]) == 64
    assert body["features"] >= 1


def test_spec_features_list_and_detail(client, git_repo):
    features = client.get("/api/spec/features").json()
    ids = {f["id"] for f in features}
    assert "FEAT-SPEC-LAYER" in ids

    detail = client.get("/api/spec/features/FEAT-SPEC-LAYER").json()
    assert detail["status"] == "approved"
    assert any(r["id"] == "REQ-001" for r in detail["requirements"])
    assert "_path" not in detail


def test_spec_feature_detail_404_for_unknown_id(client, git_repo):
    r = client.get("/api/spec/features/FEAT-DOES-NOT-EXIST")
    assert r.status_code == 404


def test_save_task_spec_rejects_unknown_classification(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Bad classification")
    r = client.post(f"/api/tasks/{tid}/spec", data={"classification": "NOT_REAL"}, follow_redirects=False)
    assert r.status_code == 409


def test_save_task_spec_round_trips_and_is_readable_via_gate_endpoint(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Round trip")
    set_spec(client, tid, classification="BEHAVIOR_CHANGE", feature_id="FEAT-SPEC-LAYER",
              requirement_ids="REQ-001", acceptance_ids="AC-001", invariant_ids="INV-001")
    t = client.get(f"/api/tasks/{tid}").json()
    assert t["spec_change_classification"] == "BEHAVIOR_CHANGE"
    assert t["spec_feature_id"] == "FEAT-SPEC-LAYER"
    assert json.loads(t["spec_requirement_ids"]) == ["REQ-001"]

    gate = client.get(f"/api/tasks/{tid}/spec-gate").json()
    assert gate["outcome"] == "PASS"


# ------------------------------------------------------------- Supervisor gate

def test_legacy_task_with_no_spec_classification_starts_unaffected(client, git_repo):
    """REQ-005: a Task nobody ever touched the Spec Layer for behaves
    exactly as before this feature existed."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = new_task(client, "Legacy task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)

    gate = client.get(f"/api/tasks/{tid}/spec-gate").json()
    assert gate["outcome"] == "NOT_APPLICABLE"

    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303


def test_behavior_change_without_spec_linkage_blocks_agent_start(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = new_task(client, "Needs a spec")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    set_spec(client, tid, classification="BEHAVIOR_CHANGE")

    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 409
    assert "SPEC_REQUIRED" in r.text

    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE workspace_id=?", (w["id"],))
    assert session is None  # no process was ever launched


def test_behavior_change_with_valid_spec_linkage_starts_and_injects_spec_context(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = new_task(client, "Properly spec-linked")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    set_spec(client, tid, classification="BEHAVIOR_CHANGE", feature_id="FEAT-SPEC-LAYER",
              requirement_ids="REQ-001", acceptance_ids="AC-001", invariant_ids="INV-001")

    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303

    prompt_row = client.app.state.db.one("SELECT content FROM prompts WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert "## SPEC" in prompt_row["content"]
    assert "FEAT-SPEC-LAYER" in prompt_row["content"]
    assert "REQ-001" in prompt_row["content"]
    assert "SPEC RULES (mandatory)" in prompt_row["content"]


def test_no_spec_linkage_means_no_spec_section_in_prompt(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    setup_fast_ready_launcher(client)
    tid = new_task(client, "Unlinked task")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)

    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r.status_code == 303

    prompt_row = client.app.state.db.one("SELECT content FROM prompts WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert "## SPEC" not in prompt_row["content"]
