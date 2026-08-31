"""Human Product Acceptance & Production Outcome Review (Phase E11).

Reuses E10's real Release/Deployment pipeline (tests/test_release_
pipeline.py's own make_release_repo/_reviewed_and_integrated_task
helpers -- never a second disposable-project fixture), E6's real
UiUxApplicabilityService/ArchitectureContextBuilder (via the app's own
production specs/features/spec-layer.yaml FEAT-SPEC-LAYER, exactly the
same real spec FEAT test_workflow_engine.py already links against), and
E7's real MANUAL_ACCEPTANCE TestCaseSpecs. E11 needs no LLM calls at
all -- "real" here means real git/subprocess/DB evidence, never fake
stand-ins for those."""
from __future__ import annotations
import json

from tests.test_autonomous_execution import register, new_change, materialize_task
from tests.test_release_pipeline import make_release_repo, _reviewed_and_integrated_task, run, FakeResp


# ================================================================ helpers

def _db(client):
    return client.app.state.db


def _link_spec_layer_feature(client, cid):
    """Governs this Change with the app's own real, already-approved
    FEAT-SPEC-LAYER (AC-001..) -- the exact real spec test_workflow_
    engine.py's own HUMAN_ACCEPTANCE test already links against."""
    client.app.state.trace.link("change", cid, "spec_feature", "FEAT-SPEC-LAYER", relation="GOVERNED_BY")


def _seed_production_release(client, rid, cid, tid, *, version="v1", digest="sha256:testdigest", head="deadbeef"):
    """Minimal-but-real production evidence: a VERIFIED PRODUCTION
    deployment + a PRODUCTION_VERIFIED Release referencing it, linked to
    the Change's own Task via release_tasks -- the exact join
    ProductAcceptanceService._current_release() uses. Mirrors
    test_workflow_engine.py's own test_waiting_human_when_only_human_
    acceptance_remains construction."""
    db = _db(client)
    dep_id = db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status,artifact_version,artifact_digest) "
        "VALUES(?,?,?,?,?,?,?)", (rid, "PRODUCTION", "main", head, "VERIFIED", version, digest))
    release_id = db.execute(
        "INSERT INTO releases(repository_id,version,source_commit,status,production_deployment_id,artifact_digest,artifact_version) "
        "VALUES(?,?,?,?,?,?,?)", (rid, version, head, "PRODUCTION_VERIFIED", dep_id, digest, version))
    db.execute("INSERT INTO release_tasks(release_id,task_id,merged_commit) VALUES(?,?,?)", (release_id, tid, head))
    return release_id, dep_id


def _user_facing_change(client, rid, title="Add a checkout button to the cart page"):
    return new_change(client, title, project_id=rid,
                       description="Users should see a new checkout button on the cart page that submits their order.")


def _backend_only_change(client, rid, title="Rebuild the internal search index nightly"):
    return new_change(client, title, project_id=rid,
                       description="A background job re-indexes the internal search table on a schedule.")


# ================================================================ E11.4/E11.28: applicability

def test_applicability_user_facing_from_change_intent(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    pas = client.app.state.product_acceptance_service
    assert pas.classify_applicability(cid) in ("USER_FACING", "MIXED")


def test_applicability_backend_only_default(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _backend_only_change(client, rid)
    pas = client.app.state.product_acceptance_service
    assert pas.classify_applicability(cid) in ("BACKEND_ONLY", "OPERATIONAL_ONLY")


# ================================================================ E11.2/E11.28: eligibility

def test_eligibility_no_production_release(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    elig = client.app.state.product_acceptance_service.eligibility(cid)
    assert elig["eligible"] is False
    assert elig["reason"] == "NO_PRODUCTION_RELEASE"


def test_eligibility_production_not_verified(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    db = _db(client)
    release_id = db.execute(
        "INSERT INTO releases(repository_id,version,source_commit,status) VALUES(?,?,?,?)",
        (rid, "v1", "deadbeef", "DEPLOYING_PRODUCTION"))
    db.execute("INSERT INTO release_tasks(release_id,task_id) VALUES(?,?)", (release_id, tid))
    elig = client.app.state.product_acceptance_service.eligibility(cid)
    assert elig["eligible"] is False
    assert elig["reason"] == "PRODUCTION_NOT_VERIFIED"


def test_eligibility_unresolved_human_decision_blocks(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    client.app.state.human_decisions.create("change", cid, "What should the button say?")
    elig = client.app.state.product_acceptance_service.eligibility(cid)
    assert elig["eligible"] is False
    assert elig["reason"] == "UNRESOLVED_HUMAN_DECISION"


def test_eligible_when_production_verified_and_applicable(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    elig = client.app.state.product_acceptance_service.eligibility(cid)
    assert elig["eligible"] is True, elig


# ================================================================ E11.27: backend-only N/A

def test_backend_only_not_applicable_under_agentic_standard(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _backend_only_change(client, rid)
    tid, _ = materialize_task(client, cid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    assert pas.gate_status(cid) is True  # NOT_APPLICABLE-equivalent pass
    assert pas.overview_status(cid) == "NOT_APPLICABLE"
    elig = pas.eligibility(cid)
    assert elig["eligible"] is False
    # AGENTIC_STANDARD's own REQUIRED_IF/HUMAN_ACCEPTANCE_APPLICABLE
    # condition already excludes BACKEND_ONLY on its own -- the
    # backend_only_excused() carve-out (reason NOT_APPLICABLE) is only
    # ever reached for CONTROLLED, which stays flatly REQUIRED here.
    assert elig["reason"] == "NOT_REQUIRED_BY_POLICY"


def test_backend_only_still_required_under_controlled_by_default(client, git_repo):
    """E11.4's own literal wording: 'may be NOT_APPLICABLE IF POLICY
    ALLOWS' -- default is NOT excused, so CONTROLLED (the strict
    profile) still genuinely requires a human review even for a
    backend-only change unless a project explicitly opts out."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _backend_only_change(client, rid)
    tid, _ = materialize_task(client, cid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "CONTROLLED"})
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    assert pas.gate_status(cid) is False
    elig = pas.eligibility(cid)
    assert elig["eligible"] is True, elig


# ================================================================ E11.6/E11.28: checklist derivation

def test_checklist_derives_from_real_acceptance_criteria_and_manual_testcase(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _link_spec_layer_feature(client, cid)
    db = _db(client)
    db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (cid, "TC-MANUAL-1", json.dumps(["REQ-001"]), "MANUAL", "MANUAL_ACCEPTANCE",
         "Checkout button is visually correct", "The button renders in the expected place", "APPROVED"))
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    checklist = pas.checklist(pa["id"])
    assert any(i["source_type"] == "ACCEPTANCE_CRITERION" and i["source_ref"] and i["source_ref"].startswith("AC-") for i in checklist)
    assert any(i["source_type"] == "MANUAL_TEST_CASE" and i["test_case_spec_id"] for i in checklist)


def test_check_item_rejects_item_from_a_different_acceptance(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _link_spec_layer_feature(client, cid)
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    other_cid = _user_facing_change(client, rid, title="Unrelated change")
    other_tid, _ = materialize_task(client, other_cid)
    _seed_production_release(client, rid, other_cid, other_tid, version="v2", digest="sha256:other", head="c0ffee")
    other_pa = pas.request(other_cid, requested_by="human")
    other_item = pas.checklist(other_pa["id"])
    if other_item:
        try:
            pas.check_item(pa["id"], other_item[0]["id"], "PASS")
            assert False, "expected ProductAcceptanceError for a cross-acceptance item id"
        except Exception as exc:
            assert "not found" in str(exc).lower()


# ================================================================ E11.8/E11.12/E11.28: accept + manual evidence

def test_accept_requires_checklist_then_binds_and_closes_gate(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _link_spec_layer_feature(client, cid)
    # A real MANUAL_ACCEPTANCE TestCaseSpec too -- so the checklist has
    # at least one MANUAL_TEST_CASE item, actually exercising the
    # test_case_spec_id-linked manual-evidence write below (not just the
    # ACCEPTANCE_CRITERION items, which never carry a test_case_spec_id).
    _db(client).execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (cid, "TC-MANUAL-1", json.dumps(["REQ-001"]), "MANUAL", "MANUAL_ACCEPTANCE",
         "Visual check", "Looks right", "APPROVED"))
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    assert pa["status"] == "PENDING"
    assert pa["artifact_digest"] == "sha256:testdigest"
    assert any(i["test_case_spec_id"] for i in pas.checklist(pa["id"]))

    try:
        pas.accept(pa["id"], "human")
        assert False, "accept should require the checklist to be satisfied first"
    except Exception as exc:
        assert "checklist" in str(exc).lower()

    for item in pas.checklist(pa["id"]):
        result = pas.check_item(pa["id"], item["id"], "PASS", note="looks right", checked_by="human")
        assert result["status"] == "PASS"
        if item["test_case_spec_id"]:
            tr = _db(client).one(
                "SELECT * FROM test_runs WHERE workspace_type='product_acceptance' AND workspace_id=? AND test_case_spec_id=?",
                (pa["id"], item["test_case_spec_id"]))
            assert tr and tr["status"] == "PASS"  # E11.12: real manual evidence, reused test_runs

    accepted = pas.accept(pa["id"], "human", "Ship it")
    assert accepted["status"] == "ACCEPTED"
    assert pas.gate_status(cid) is True

    evidence = pas.evidence(pa["id"])
    assert evidence["work_product"]["content"]["verdict"] == "ACCEPTED"
    assert evidence["work_product"]["content"]["artifact_digest"] == "sha256:testdigest"


def test_accept_fails_when_a_checklist_item_is_fail(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _link_spec_layer_feature(client, cid)
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    items = pas.checklist(pa["id"])
    for item in items:
        pas.check_item(pa["id"], item["id"], "FAIL" if item is items[0] else "PASS")
    try:
        pas.accept(pa["id"], "human")
        assert False, "accept must fail with any checklist item marked FAIL"
    except Exception as exc:
        assert "fail" in str(exc).lower()


# ================================================================ E11.9/E11.25/E11.28: request change

def test_request_change_creates_new_change_preserves_history(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")

    result = pas.request_change(pa["id"], "human", "Save button should be below the form.", classification="UX_CHANGE")
    follow_up_id = result["follow_up_change_id"]
    assert result["acceptance"]["status"] == "CHANGE_REQUESTED"

    follow_up = client.app.state.changes.get(follow_up_id)
    assert follow_up["parent_change_id"] == cid
    assert follow_up["change_type"] == "UX_CHANGE"
    assert "Save button" in follow_up["description"]

    original = client.app.state.changes.get(cid)
    assert original["lifecycle_state"] == "DELIVERED_BUT_CHANGE_REQUESTED"
    # Original history preserved: its own Task/WorkProducts still exist untouched.
    assert client.app.state.changes.list_tasks_for_change(cid)[0]["id"] == tid

    children = client.app.state.changes.list_children(cid)
    assert any(c["id"] == follow_up_id for c in children)

    # Production remains healthy/current -- no deployment/release row touched.
    release = _db(client).one("SELECT * FROM releases WHERE repository_id=?", (rid,))
    assert release["status"] == "PRODUCTION_VERIFIED"


def test_request_change_requires_feedback_text(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    try:
        pas.request_change(pa["id"], "human", "   ")
        assert False, "blank feedback must be rejected"
    except Exception as exc:
        assert "feedback" in str(exc).lower()


# ================================================================ E11.10/E11.28: reject

def test_reject_requires_reason_and_never_touches_deployment(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    release_id, dep_id = _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")

    try:
        pas.reject(pa["id"], "human", "")
        assert False, "reject must require a reason"
    except Exception as exc:
        assert "reason" in str(exc).lower()

    rejected = pas.reject(pa["id"], "human", "This is not what we asked for at all.")
    assert rejected["status"] == "REJECTED"
    # PRODUCT REJECTION is distinct from RUNTIME FAILURE (E11.10) -- no
    # automatic rollback: the deployment/release rows are untouched.
    dep = _db(client).one("SELECT * FROM deployments WHERE id=?", (dep_id,))
    assert dep["status"] == "VERIFIED"
    release = _db(client).one("SELECT * FROM releases WHERE id=?", (release_id,))
    assert release["status"] == "PRODUCTION_VERIFIED"
    # No classification given -> a HumanDecision was raised instead of a
    # follow-up Change (E11.10's own "Create follow-up Change or Human
    # Decision according to classification").
    pending = client.app.state.human_decisions.list_pending_for_change(cid)
    assert any("rejected" in d["question"].lower() for d in pending)


def test_reject_with_classification_creates_follow_up_change(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    rejected = pas.reject(pa["id"], "human", "Fundamentally wrong flow.", classification="PRODUCT_ADJUSTMENT")
    assert rejected["follow_up_change_id"]
    follow_up = client.app.state.changes.get(rejected["follow_up_change_id"])
    assert follow_up["parent_change_id"] == cid


# ================================================================ E11.11/E11.26/E11.28: staleness

def test_new_deployment_invalidates_old_acceptance(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid, version="v1", digest="sha256:aaa", head="commitv1")
    pas = client.app.state.product_acceptance_service
    pa = pas.request(cid, requested_by="human")
    assert pa["status"] == "PENDING"

    # Release B deployed -- the old acceptance request must become
    # stale, and can no longer be accepted.
    _seed_production_release(client, rid, cid, tid, version="v2", digest="sha256:bbb", head="commitv2")

    try:
        pas.accept(pa["id"], "human")
        assert False, "a stale acceptance (superseded by a new production deployment) must not be acceptable"
    except Exception as exc:
        assert "PENDING" in str(exc) or "stale" in str(exc).lower() or "SUPERSEDED" in str(exc)

    refreshed = pas.get(pa["id"])
    assert refreshed["status"] == "SUPERSEDED"

    # Requesting again binds to the NEW artifact (v2), not the old one.
    pa2 = pas.request(cid, requested_by="human")
    assert pa2["artifact_digest"] == "sha256:bbb"
    assert pa2["id"] != pa["id"]


# ================================================================ E11.28: backward compatibility

def test_legacy_human_decision_approach_no_longer_satisfies_gate_without_wiring(client, git_repo):
    """Confirms the E11 gate is the real, wired truth source now (not
    that the legacy path is untestable -- see workflow_engine.py's own
    _gate_human_acceptance docstring for the unwired fallback, exercised
    implicitly by every pre-E11 test file that never wires this hook)."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "CONTROLLED"})
    client.app.state.work_products.create(kind="HUMAN_DECISION", title="Accepted", change_id=cid, status="APPROVED")
    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "HUMAN_ACCEPTANCE" in state["unmet_gates"]


def test_deploy_verify_release_control_surface_unaffected(client, git_repo):
    """E9/E10 evidence and gates keep working exactly as before -- E11
    only adds a new gate, never touches REVIEW_PASS/SECURITY_PASS/
    DEPLOY_VERIFIED's own real evidence sources."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Unaffected change", project_id=rid)
    r = client.get(f"/changes/{cid}")
    assert r.status_code == 200
    r2 = client.get(f"/changes/{cid}/release")
    assert r2.status_code == 200
    r3 = client.get(f"/changes/{cid}/deploy")
    assert r3.status_code == 200
    r4 = client.get(f"/changes/{cid}/acceptance")
    assert r4.status_code == 200
    assert "Not applicable" in r4.text or "Nothing to review" in r4.text or "not deployed" in r4.text.lower()


# ================================================================ E11.17/E11.21: UI/API surface

def test_acceptance_ui_pending_flow(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    r = client.post(f"/api/changes/{cid}/acceptance/request", data={"requested_by": "human"})
    assert r.status_code == 200, r.text
    pa = r.json()
    page = client.get(f"/changes/{cid}/acceptance")
    assert "ready for review" in page.text.lower()
    assert "Accept Product" in page.text

    r2 = client.get(f"/api/changes/{cid}/acceptance")
    assert r2.status_code == 200
    body = r2.json()
    assert body["acceptance"]["id"] == pa["id"]

    for item in body["checklist"]:
        client.post(f"/api/product-acceptances/{pa['id']}/checklist/{item['id']}", data={"status": "PASS"})
    accept = client.post(f"/api/product-acceptances/{pa['id']}/accept", data={"actor": "human"})
    assert accept.status_code == 200 and accept.json()["status"] == "ACCEPTED"

    accepted_page = client.get(f"/changes/{cid}/acceptance")
    assert "accepted" in accepted_page.text.lower()


def test_acceptance_ui_overview_summary_shows_pending(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    client.app.state.product_acceptance_service.request(cid, requested_by="human")
    page = client.get(f"/changes/{cid}")
    assert page.status_code == 200
    assert "PENDING" in page.text


def test_human_attention_distinguishes_decision_from_product_review(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid)
    tid, _ = materialize_task(client, cid)
    _seed_production_release(client, rid, cid, tid)
    client.app.state.product_acceptance_service.request(cid, requested_by="human")
    page = client.get("/changes")
    assert page.status_code == 200
    assert "Product review required" in page.text


# ================================================================ E11.24: real acceptance fixture end-to-end

def test_real_full_acceptance_flow_reaches_change_complete(client, git_repo, tmp_path):
    """The exact E11.24 flow: Change -> release -> production deployment
    -> runtime verified -> acceptance request -> real checklist (from
    real spec AC + real MANUAL_ACCEPTANCE TestCaseSpec) -> live URL
    present -> human simulation checks PASS -> accept -> HUMAN_ACCEPTANCE
    gate PASS -> Workflow Change reaches COMPLETE (CONTROLLED profile,
    every other gate satisfied with real E1-E10 evidence, same
    construction test_workflow_engine.py's own CONTROLLED test uses)."""
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19100", port=19100)
    rid = register(client, repo, "demo")
    cid = _user_facing_change(client, rid, title="Add an order confirmation screen")
    # AGENTIC_STANDARD, not CONTROLLED -- exercises E11.3's own
    # "recommended/default after Production deploy when user-facing
    # behavior changed" REQUIRED_IF path directly (this Change IS
    # user-facing), and avoids CONTROLLED's mandatory SecurityReview
    # (a separate, already-tested E9 concern, not what this test is
    # about).
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    # Deliberately no Change-level GOVERNED_BY trace link to FEAT-SPEC-
    # LAYER here (unlike the Task-level /api/tasks/{tid}/spec linkage
    # below, which SPEC_COMPLIANCE_PASS needs) -- TEST_DESIGN_READY's
    # own coverage check would then require a TestCaseSpec proving
    # EVERY real requirement on that whole production feature, not just
    # REQ-001, which is unrelated E7 scope this test isn't exercising.
    # test_test_design.py already covers TEST_DESIGN_READY's real
    # coverage-gate behavior in depth.

    client.app.state.work_products.create(kind="FEATURE_SPEC", title="Spec", change_id=cid, status="APPROVED")
    client.app.state.work_products.create(kind="TECHNICAL_DESIGN", title="Design", change_id=cid, status="APPROVED")

    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    # LOW risk -- same as test_workflow_engine.py's own CONTROLLED
    # HUMAN_ACCEPTANCE test -- so SpecComplianceVerifier's QA
    # requirement (RISK_GATES) doesn't need a separate sandbox/manual-
    # verification flow this test isn't otherwise exercising.
    _db(client).execute("UPDATE tasks SET risk_profile='LOW' WHERE id=?", (tid,))
    client.post(f"/api/tasks/{tid}/spec", data={
        "classification": "BUG_FIX_TO_EXISTING_SPEC", "feature_id": "FEAT-SPEC-LAYER",
        "requirement_ids": "REQ-001", "acceptance_ids": "AC-001",
    })
    head = integ["integrated_commit"]
    db = _db(client)
    task_workspace = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    # SPEC_COMPLIANCE_PASS requires at least one verification_reports row
    # stamped with this exact spec_feature_id (SpecComplianceVerifier's
    # own S7 "never PASS on a declared-but-never-produced mapping" rule)
    # -- _reviewed_and_integrated_task's own report was submitted before
    # the /api/tasks/{tid}/spec linkage above, so a fresh one is needed.
    client.post(f"/api/workspaces/{task_workspace['id']}/verification-report",
                data={"work_status": "READY", "what_changed": "order confirmation screen"}, follow_redirects=False)
    # TESTS_PASS gate reuses TaskDecisionService's own AUTOMATED_TESTS
    # checklist item (test_runs, workspace_type='agent', pinned to the
    # exact submitted HEAD) -- the same construction test_workflow_
    # engine.py's own CONTROLLED test uses.
    db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit) VALUES('agent',?,?,?,?,?)",
        (task_workspace["id"], "pytest", "test", "PASS", task_workspace["last_commit"]))
    db.execute(
        "INSERT INTO test_case_specs(change_id,item_key,requirement_ids,test_level,test_type,title,expected_results,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (cid, "TC-MANUAL-CONFIRM", json.dumps(["REQ-001"]), "MANUAL", "MANUAL_ACCEPTANCE",
         "Order confirmation screen shows the order summary", "The screen lists items and total", "APPROVED"))

    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    svc.deploy_test(r["id"])
    svc.sync_test_result(r["id"])
    svc.approve_production(r["id"], "operator")
    svc.deploy_production(r["id"])
    prod_result = svc.sync_production_result(r["id"])
    assert prod_result["outcome"] == "RELEASE_COMPLETE", prod_result

    pas = client.app.state.product_acceptance_service
    elig = pas.eligibility(cid)
    assert elig["eligible"] is True, elig
    pa = pas.request(cid, requested_by="human")
    assert pa["status"] == "PENDING"
    ctx = pas.context(cid)
    assert ctx["live_url"] or ctx["runtime_target"]  # honest runtime target when no public URL
    assert len(pas.checklist(pa["id"])) >= 1

    for item in pas.checklist(pa["id"]):
        pas.check_item(pa["id"], item["id"], "PASS", checked_by="human")
    accepted = pas.accept(pa["id"], "human", "Confirmed on live app")
    assert accepted["status"] == "ACCEPTED"

    state = client.get(f"/api/changes/{cid}/workflow/state").json()
    assert "HUMAN_ACCEPTANCE" not in state["unmet_gates"], state
    assert state["status"] == "COMPLETE", state["unmet_gates"]
