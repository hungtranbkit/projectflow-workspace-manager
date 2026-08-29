"""Spec Layer V1 (S7/S8): SpecComplianceVerifier produces a
SpecComplianceResult built entirely from real evidence ProjectFlow
already collects (TaskDecisionService.evaluate() + EvidenceStore) --
never a second, independently-derived readiness calculation, and it
never emits PASS when required evidence is missing (REQ-004)."""
from __future__ import annotations
import json


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def new_task(client, title, risk="LOW"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def add_workspace(client, tid, rid, agent="codex", role="Backend"):
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": agent, "role": role, "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent and w["role"] == role][-1]


def submit_for_review(client, w):
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})
    assert r.status_code in (200, 303)


def review(client, w, result="PASS", reviewer="claude"):
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": reviewer})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": result}, follow_redirects=False)
    assert r.status_code == 303


def set_spec(client, tid, **fields):
    data = {"classification": "", "feature_id": "", "spec_version": "", "requirement_ids": "", "acceptance_ids": "", "invariant_ids": ""}
    data.update(fields)
    r = client.post(f"/api/tasks/{tid}/spec", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text


def compliance(client, tid):
    return client.get(f"/api/tasks/{tid}/spec-compliance").json()


def evidence(client, tid):
    return client.get(f"/api/tasks/{tid}/evidence").json()


def setup(client, git_repo, title, risk="LOW"):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, title, risk=risk)
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    return tid, w


def test_compliance_is_incomplete_before_any_review(client, git_repo):
    tid, w = setup(client, git_repo, "No review yet")
    result = compliance(client, tid)
    assert result["gate_outcome"] == "NOT_APPLICABLE"
    assert result["verdict"] == "INCOMPLETE"


def test_compliance_passes_for_a_legacy_task_with_no_spec_linkage(client, git_repo):
    """A Task that never engaged with the Spec Layer at all still gets a
    real, useful verdict once its evidence is actually complete --
    spec linkage is never required for an ordinary PASS."""
    tid, w = setup(client, git_repo, "Legacy compliant task")
    submit_for_review(client, w)
    review(client, w, "PASS")
    result = compliance(client, tid)
    assert result["gate_outcome"] == "NOT_APPLICABLE"
    assert result["verdict"] == "PASS"
    assert result["evidence"]["reviews_pass"] is True


def test_compliance_fails_when_review_reports_fix_required(client, git_repo):
    tid, w = setup(client, git_repo, "Needs fixes")
    submit_for_review(client, w)
    review(client, w, "FIX_REQUIRED")
    result = compliance(client, tid)
    assert result["verdict"] == "FAIL"


def test_compliance_is_incomplete_when_spec_required_but_unmet(client, git_repo):
    tid, w = setup(client, git_repo, "Behavior change, no linkage")
    set_spec(client, tid, classification="BEHAVIOR_CHANGE")
    result = compliance(client, tid)
    assert result["gate_outcome"] == "SPEC_REQUIRED"
    assert result["verdict"] == "INCOMPLETE"


def test_compliance_requires_traced_evidence_for_a_spec_linked_task(client, git_repo):
    """REQ-004: never PASS on a declared-but-never-produced mapping --
    linking a Task to a feature is not itself evidence."""
    tid, w = setup(client, git_repo, "Linked but no evidence yet")
    set_spec(client, tid, classification="BEHAVIOR_CHANGE", feature_id="FEAT-SPEC-LAYER",
             requirement_ids="REQ-001", acceptance_ids="AC-001")
    result = compliance(client, tid)
    assert result["gate_outcome"] == "PASS"
    assert result["verdict"] == "INCOMPLETE"


def test_compliance_passes_with_traced_evidence_for_a_spec_linked_task(client, git_repo):
    tid, w = setup(client, git_repo, "Fully spec-linked and verified")
    set_spec(client, tid, classification="BEHAVIOR_CHANGE", feature_id="FEAT-SPEC-LAYER",
             requirement_ids="REQ-001", acceptance_ids="AC-001", invariant_ids="INV-001")
    submit_for_review(client, w)  # snapshots spec_* onto verification_reports (S8)
    review(client, w, "PASS")

    result = compliance(client, tid)
    assert result["verdict"] == "PASS"
    assert result["traceability"]["linked_evidence_count"] >= 1

    ev = evidence(client, tid)
    assert len(ev["verification_reports"]) >= 1
    assert ev["verification_reports"][0]["spec_feature_id"] == "FEAT-SPEC-LAYER"
    assert json.loads(ev["verification_reports"][0]["spec_requirement_ids"]) == ["REQ-001"]
    assert len(ev["review_runs"]) >= 1


def test_compliance_reports_spec_drift_when_linkage_becomes_invalid(client, git_repo):
    """S9/AC-007: a Task whose spec reference SpecRegistry can no longer
    resolve is SPEC_DRIFT, not silently downgraded to INCOMPLETE, even
    if the Task's own evidence otherwise looks complete."""
    tid, w = setup(client, git_repo, "Later becomes drifted")
    set_spec(client, tid, classification="BEHAVIOR_CHANGE", feature_id="FEAT-SPEC-LAYER",
             requirement_ids="REQ-001", acceptance_ids="AC-001")
    submit_for_review(client, w)
    review(client, w, "PASS")
    assert compliance(client, tid)["verdict"] == "PASS"

    # simulate the spec having moved on / a stale reference, without
    # going through the write-time-permissive /spec route again.
    client.app.state.db.execute(
        "UPDATE tasks SET spec_requirement_ids=? WHERE id=?",
        (json.dumps(["REQ-DOES-NOT-EXIST"]), tid),
    )
    result = compliance(client, tid)
    assert result["gate_outcome"] == "SPEC_REFERENCE_INVALID"
    assert result["verdict"] == "SPEC_DRIFT"


def test_evidence_store_returns_empty_shape_for_an_untouched_task(client, git_repo):
    tid, w = setup(client, git_repo, "No evidence yet")
    ev = evidence(client, tid)
    assert ev == {"verification_reports": [], "review_runs": [], "qa_runs": [], "test_runs": [], "manual_verifications": []}
