"""Bug / Incident Closed Loop (Phase E12).

Production/User Feedback/Monitoring -> Incident/Bug -> Classify -> Link
existing Spec/Requirement -> Spec gap if needed -> Reproduce ->
Regression Test -> Plan/Fix -> Review -> Deploy -> Verify incident
resolved -> Close.

Reuses E1's Change domain (the incident's own fix_change_id), E5's
SpecLifecycleService/SpecRegistry (real production specs/features/
spec-layer.yaml FEAT-SPEC-LAYER, the exact same feature test_workflow_
engine.py/test_product_acceptance.py already link against), E7's real
TestCaseSpecStore, E9/E10's real WorkflowService/ReleaseService state,
and the existing test_runs table for real regression-test evidence
(workspace_type='incident') -- E12 needs no LLM calls at all."""
from __future__ import annotations
import json

from tests.test_autonomous_execution import register, new_change, materialize_task
from tests.test_release_pipeline import make_release_repo, _reviewed_and_integrated_task, FakeResp


def _db(client):
    return client.app.state.db


def _svc(client):
    return client.app.state.incident_service


# ================================================================ report/classify

def test_report_creates_incident_and_evidence(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    svc = _svc(client)
    inc = svc.report("Checkout crashes on submit", "500 error", source="PRODUCTION", severity="HIGH",
                      reported_by="oncall", project_id=rid)
    assert inc["status"] == "REPORTED"
    assert inc["severity"] == "HIGH"
    assert inc["work_product_id"]
    wp = client.app.state.work_products.get(inc["work_product_id"])
    assert wp["kind"] == "INCIDENT_REPORT"


def test_report_rejects_unknown_source(client, git_repo):
    svc = _svc(client)
    try:
        svc.report("X", source="CARRIER_PIGEON")
        assert False, "unknown source must be rejected"
    except Exception as exc:
        assert "source" in str(exc).lower()


def test_classify_materializes_fix_change(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    svc = _svc(client)
    inc = svc.report("Search returns wrong results", project_id=rid)
    classified = svc.classify(inc["id"], "BUG")
    assert classified["status"] == "CLASSIFIED"
    assert classified["change_id"]
    change = client.app.state.changes.get(classified["change_id"])
    assert change["change_type"] == "BUG"


def test_classify_security_maps_to_security_change_type(client, git_repo):
    svc = _svc(client)
    inc = svc.report("Auth bypass found in production")
    classified = svc.classify(inc["id"], "SECURITY", severity="CRITICAL")
    change = client.app.state.changes.get(classified["change_id"])
    assert change["change_type"] == "SECURITY_CHANGE"
    assert change["risk_level"] == "HIGH"


def test_classify_rejects_unknown_classification(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    try:
        svc.classify(inc["id"], "ALIEN_INVASION")
        assert False
    except Exception as exc:
        assert "classification" in str(exc).lower()


# ================================================================ link spec / spec gap

def test_link_spec_real_feature(client, git_repo):
    svc = _svc(client)
    inc = svc.report("SpecGate lets an unlinked Task start")
    inc = svc.classify(inc["id"], "BUG")
    linked = svc.link_spec(inc["id"], "FEAT-SPEC-LAYER", requirement_ids=["REQ-001"], acceptance_ids=["AC-001"])
    assert linked["status"] == "SPEC_LINKED"
    assert linked["spec_feature_id"] == "FEAT-SPEC-LAYER"
    links = client.app.state.trace.for_source("change", linked["change_id"])
    assert any(l["target_type"] == "spec_feature" and l["target_id"] == "FEAT-SPEC-LAYER" for l in links)


def test_link_spec_unknown_feature_rejected(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    try:
        svc.link_spec(inc["id"], "FEAT-DOES-NOT-EXIST")
        assert False
    except Exception as exc:
        assert "unknown spec feature" in str(exc).lower()


def test_link_spec_before_classify_rejected(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    try:
        svc.link_spec(inc["id"], "FEAT-SPEC-LAYER")
        assert False, "cannot link a spec before the fix Change exists"
    except Exception as exc:
        assert "classify" in str(exc).lower()


def test_spec_gap_auto_resolves_once_proposal_applied(client, git_repo):
    svc = _svc(client)
    inc = svc.report("No spec covers this behavior at all")
    inc = svc.classify(inc["id"], "BUG")
    inc = svc.mark_spec_gap(inc["id"], "Nothing in the spec describes this")
    assert inc["status"] == "SPEC_GAP_PENDING"
    unchanged = svc.sync_spec_gap(inc["id"])
    assert unchanged["status"] == "SPEC_GAP_PENDING"  # no proposal yet -> no change

    # Real spec_proposals row, APPLIED -- the exact SpecLifecycleService
    # status this same real Spec Layer machinery produces (E5).
    db = _db(client)
    db.execute(
        "INSERT INTO spec_proposals(change_id,feature_id,proposed_version,status,author_provider,input_context_digest,proposed_content) "
        "VALUES(?,?,?,?,?,?,?)",
        (inc["change_id"], "FEAT-SPEC-LAYER", 2, "APPLIED", "claude", "x", "{}"))
    resolved = svc.sync_spec_gap(inc["id"])
    assert resolved["status"] == "SPEC_LINKED"
    assert resolved["spec_gap_proposal_id"]


# ================================================================ reproduce

def test_reproduction_flow_reproduced(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    inc = svc.link_spec(inc["id"], "FEAT-SPEC-LAYER")
    inc = svc.start_reproduction(inc["id"])
    assert inc["status"] == "REPRODUCING"
    inc = svc.record_reproduction(inc["id"], True, "Confirmed: 500 on invalid quantity", commit="abc123")
    assert inc["status"] == "REPRODUCED"
    assert inc["reproduced_commit"] == "abc123"


def test_reproduction_flow_cannot_reproduce_then_close(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], False, "Could not trigger it")
    assert inc["status"] == "CANNOT_REPRODUCE"
    closed = svc.close(inc["id"], "human", "Not reproducible; monitoring")
    assert closed["status"] == "CLOSED"


def test_record_reproduction_requires_reproducing_state(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    try:
        svc.record_reproduction(inc["id"], True, "n/a")
        assert False
    except Exception as exc:
        assert "REPRODUCING" in str(exc)


# ================================================================ regression test + real evidence

def test_regression_test_requires_reproduced_and_real_test_case_spec(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    db = _db(client)
    tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (inc["change_id"], "TC-REPRO-1", json.dumps([]), "UNIT", "NEGATIVE",
         "Invalid quantity is rejected", "A 400 is returned, not a 500", "APPROVED"))
    try:
        svc.add_regression_test(inc["id"], tcsid)
        assert False, "must require REPRODUCED status first"
    except Exception as exc:
        assert "REPRODUCED" in str(exc)

    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], True, "confirmed")
    added = svc.add_regression_test(inc["id"], tcsid)
    assert added["status"] == "REGRESSION_TEST_ADDED"
    assert added["regression_test_case_spec_id"] == tcsid


def test_regression_test_rejects_spec_from_a_different_change(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], True, "confirmed")
    other_change = new_change(client, "Unrelated change")
    db = _db(client)
    other_tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (other_change, "TC-X", json.dumps([]), "UNIT", "POSITIVE", "Unrelated", "n/a", "APPROVED"))
    try:
        svc.add_regression_test(inc["id"], other_tcsid)
        assert False
    except Exception as exc:
        assert "own fix change" in str(exc).lower()


def test_record_regression_result_writes_real_test_runs_evidence(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    db = _db(client)
    tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (inc["change_id"], "TC-1", json.dumps([]), "UNIT", "NEGATIVE", "Repro test", "no 500", "APPROVED"))
    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], True, "confirmed", commit="badcommit")
    inc = svc.add_regression_test(inc["id"], tcsid)

    svc.record_regression_result(inc["id"], "FAIL", "badcommit")
    history = svc.regression_history(inc["id"])
    assert len(history) == 1 and history[0]["status"] == "FAIL" and history[0]["tested_commit"] == "badcommit"
    tr = db.one("SELECT * FROM test_runs WHERE workspace_type='incident' AND workspace_id=?", (inc["id"],))
    assert tr and tr["test_case_spec_id"] == tcsid


# ================================================================ plan/fix -> review -> deploy (composition-only sync)

def test_sync_status_advances_through_plan_review_deploy(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    svc = _svc(client)
    inc = svc.report("X", project_id=rid)
    inc = svc.classify(inc["id"], "BUG")
    client.post(f"/api/changes/{inc['change_id']}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    inc = svc.link_spec(inc["id"], "FEAT-SPEC-LAYER")
    db = _db(client)
    tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (inc["change_id"], "TC-1", json.dumps([]), "UNIT", "NEGATIVE", "Repro test", "no 500", "APPROVED"))
    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], True, "confirmed", commit="v0")
    inc = svc.add_regression_test(inc["id"], tcsid)
    svc.record_regression_result(inc["id"], "FAIL", "v0")

    unchanged = svc.sync_status(inc["id"])
    assert unchanged["status"] == "REGRESSION_TEST_ADDED"  # no Task yet

    tid, _ = materialize_task(client, inc["change_id"])
    after_task = svc.sync_status(inc["id"])
    assert after_task["status"] == "FIX_PLANNED"

    # REVIEW_PASS (E9 real evidence, same fake-invoker-free path
    # test_release_pipeline.py's own helper uses).
    from tests.test_review_fix_loop import set_fake, PASS
    from tests.test_worktree_manager import _select_and_create_workspace
    ws = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(ws["worktree_path"])
    (worktree / "fix.py").write_text("def ok():\n    return True\n")
    import subprocess
    subprocess.run(["git", "add", "fix.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{ws['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    review = client.post(f"/api/tasks/{tid}/review/code").json()
    assert review["outcome"] == "REVIEWED" and review["verdict"] == "PASS", review

    after_review = svc.sync_status(inc["id"])
    assert after_review["status"] == "FIX_REVIEWED"

    # A real PRODUCTION_VERIFIED Release for this Task -- same minimal-
    # but-real construction test_workflow_engine.py/test_product_
    # acceptance.py already use.
    dep_id = db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status,artifact_version,artifact_digest) "
        "VALUES(?,?,?,?,?,?,?)", (rid, "PRODUCTION", "main", "v1commit", "VERIFIED", "v1", "sha256:fixdigest"))
    release_id = db.execute(
        "INSERT INTO releases(repository_id,version,source_commit,status,production_deployment_id,artifact_digest,artifact_version) "
        "VALUES(?,?,?,?,?,?,?)", (rid, "v1", "v1commit", "PRODUCTION_VERIFIED", dep_id, "sha256:fixdigest", "v1"))
    db.execute("INSERT INTO release_tasks(release_id,task_id) VALUES(?,?)", (release_id, tid))

    deployed = svc.sync_status(inc["id"])
    assert deployed["status"] == "DEPLOYED"
    assert deployed["resolved_release_id"] == release_id
    assert deployed["resolved_deployment_id"] == dep_id


# ================================================================ verify resolved -- artifact-bound

def test_verify_resolved_requires_pass_at_the_resolving_commit(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    svc = _svc(client)
    inc = svc.report("X", project_id=rid)
    inc = svc.classify(inc["id"], "BUG")
    db = _db(client)
    tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (inc["change_id"], "TC-1", json.dumps([]), "UNIT", "NEGATIVE", "Repro test", "no 500", "APPROVED"))
    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], True, "confirmed", commit="v0")
    inc = svc.add_regression_test(inc["id"], tcsid)
    svc.record_regression_result(inc["id"], "FAIL", "v0")

    tid, _ = materialize_task(client, inc["change_id"])
    dep_id = db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status,artifact_version,artifact_digest) "
        "VALUES(?,?,?,?,?,?,?)", (rid, "PRODUCTION", "main", "v1commit", "VERIFIED", "v1", "sha256:fixdigest"))
    release_id = db.execute(
        "INSERT INTO releases(repository_id,version,source_commit,status,production_deployment_id,artifact_digest,artifact_version) "
        "VALUES(?,?,?,?,?,?,?)", (rid, "v1", "v1commit", "PRODUCTION_VERIFIED", dep_id, "sha256:fixdigest", "v1"))
    db.execute("INSERT INTO release_tasks(release_id,task_id) VALUES(?,?)", (release_id, tid))
    inc = svc.sync_status(inc["id"])
    assert inc["status"] == "DEPLOYED"

    # No PASS evidence at all yet -> VERIFICATION_FAILED, never a
    # default PASS just because a deployment happened. Re-callable
    # (VERIFICATION_FAILED, not just DEPLOYED) so a human can retry
    # once real evidence exists, without an extra sync_status() detour.
    no_evidence = svc.verify_resolved(inc["id"])
    assert no_evidence["status"] == "VERIFICATION_FAILED"

    # Stale evidence (PASS, but at the WRONG commit) must not count.
    svc.record_regression_result(inc["id"], "PASS", "some-other-commit")
    failed = svc.verify_resolved(inc["id"])
    assert failed["status"] == "VERIFICATION_FAILED"

    # Real evidence at the exact resolving commit -> VERIFIED.
    svc.record_regression_result(inc["id"], "PASS", "v1commit")
    verified = svc.verify_resolved(inc["id"])
    assert verified["status"] == "VERIFIED"
    assert verified["verified_at"]

    closed = svc.close(verified["id"], "human", "Confirmed fixed in production")
    assert closed["status"] == "CLOSED"


def test_close_requires_verified_or_cannot_reproduce(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    try:
        svc.close(inc["id"], "human")
        assert False
    except Exception as exc:
        assert "VERIFIED" in str(exc)


def test_reopen_requires_reason_and_clears_resolution(client, git_repo):
    svc = _svc(client)
    inc = svc.report("X")
    inc = svc.classify(inc["id"], "BUG")
    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], False, "n/a")
    closed = svc.close(inc["id"], "human")
    assert closed["status"] == "CLOSED"
    try:
        svc.reopen(closed["id"], "")
        assert False, "reopen must require a reason"
    except Exception as exc:
        assert "reason" in str(exc).lower()
    reopened = svc.reopen(closed["id"], "It happened again in production")
    assert reopened["status"] == "REOPENED"
    assert reopened["closed_at"] is None


# ================================================================ UI/API surface

def test_incidents_pages_load(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    svc = _svc(client)
    inc = svc.report("Login page throws 500", project_id=rid)
    r = client.get("/incidents")
    assert r.status_code == 200 and "Login page throws 500" in r.text
    r2 = client.get(f"/incidents/{inc['id']}")
    assert r2.status_code == 200
    r3 = client.get(f"/api/incidents/{inc['id']}")
    assert r3.status_code == 200 and r3.json()["title"] == inc["title"]


def test_report_and_classify_via_http_api(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/incidents", data={"title": "Payments fail intermittently", "source": "MONITORING",
                                              "severity": "CRITICAL", "project_id": str(rid)})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    r2 = client.post(f"/api/incidents/{iid}/classify", data={"classification": "BUG"})
    assert r2.status_code == 200 and r2.json()["status"] == "CLASSIFIED"
    r3 = client.post(f"/api/incidents/{iid}/reproduction/start")
    assert r3.status_code == 200
    r4 = client.post(f"/api/incidents/{iid}/reproduction/record", data={"reproduced": "true", "note": "confirmed"})
    assert r4.status_code == 200 and r4.json()["status"] == "REPRODUCED"


# ================================================================ real end-to-end closed loop

def test_real_full_incident_closed_loop_reaches_closed(client, git_repo, tmp_path):
    """The exact E12 flow with real production evidence throughout:
    report -> classify -> link real spec -> reproduce -> real regression
    TestCaseSpec with a real FAIL test_runs row -> real Task/Review (E9)
    -> real Release/Production Deployment (E10) -> sync -> real PASS
    regression evidence at the resolving artifact's own commit ->
    verify resolved -> close."""
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19200", port=19200)
    rid = register(client, repo, "demo")
    svc = _svc(client)

    inc = svc.report("Feature endpoint returns stale data", source="PRODUCTION", severity="HIGH", project_id=rid)
    inc = svc.classify(inc["id"], "BUG")
    inc = svc.link_spec(inc["id"], "FEAT-SPEC-LAYER", requirement_ids=["REQ-001"])
    inc = svc.start_reproduction(inc["id"])
    inc = svc.record_reproduction(inc["id"], True, "Confirmed stale response on repeated GET", commit="deadbeef")

    db = _db(client)
    tcsid = db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (inc["change_id"], "TC-STALE-1", json.dumps(["REQ-001"]), "INTEGRATION", "REGRESSION",
         "Endpoint no longer returns stale data", "Response reflects the latest write", "APPROVED"))
    inc = svc.add_regression_test(inc["id"], tcsid)
    svc.record_regression_result(inc["id"], "FAIL", "deadbeef")

    tid, integ = _reviewed_and_integrated_task(client, repo, inc["change_id"], rid, title="Fix stale response")

    release_svc = client.app.state.release_service
    r = release_svc.create_release(rid, [tid], version="v1")
    release_svc.build(r["id"])
    release_svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    release_svc.deploy_test(r["id"])
    release_svc.sync_test_result(r["id"])
    release_svc.approve_production(r["id"], "operator")
    release_svc.deploy_production(r["id"])
    prod_result = release_svc.sync_production_result(r["id"])
    assert prod_result["outcome"] == "RELEASE_COMPLETE", prod_result

    synced = svc.sync_status(inc["id"])
    assert synced["status"] == "DEPLOYED", synced
    release = release_svc.get(synced["resolved_release_id"])

    # Real fresh PASS evidence at the exact shipped commit.
    svc.record_regression_result(inc["id"], "PASS", release["source_commit"])
    verified = svc.verify_resolved(inc["id"])
    assert verified["status"] == "VERIFIED", verified

    closed = svc.close(verified["id"], "human", "Verified fixed in production")
    assert closed["status"] == "CLOSED"

    evidence = client.app.state.work_products.get(closed["work_product_id"])
    assert evidence["kind"] == "INCIDENT_REPORT"
    content = json.loads(evidence["content_metadata"])
    assert content["verdict"] == "CLOSED"
    assert content["resolved_release_id"] == r["id"]

    page = client.get(f"/incidents/{closed['id']}")
    assert page.status_code == 200 and "CLOSED" in page.text
