"""Autonomous Spec Lifecycle (Phase E5). SAFETY: every test in this
file redirects every spec-lifecycle service's `specs_root` to an
isolated tmp_path tree via the `spec_env` fixture below -- the real
`create_app()` factory always resolves specs_root to THIS repo's own
real specs/ directory (by design, see app/main.py), so without this
redirection any test that calls SpecAuthorService/SpecProposalValidator/
SpecLifecycleService.apply_proposal would write into (or read stale
context from) the real, live specs/ tree. Never remove this fixture
from a test that touches proposal creation/validation/apply."""
from __future__ import annotations
import json

import pytest
import yaml

from app.launchers import AgentLauncher
from app.services.spec_lifecycle_service import SPEC_PROPOSAL_STATUSES


@pytest.fixture
def spec_env(client, tmp_path):
    """An isolated, disposable specs/ tree (same shape as the real
    repo's) with one pre-existing approved feature -- and every
    spec-lifecycle service on `client.app.state` redirected to it."""
    specs_root = tmp_path / "specs"
    (specs_root / "features").mkdir(parents=True)
    (specs_root / "SPEC.yaml").write_text("schema_version: 1\nproject: test\nglossary: glossary.yaml\nfeatures_dir: features\n")
    (specs_root / "glossary.yaml").write_text("schema_version: 1\nterms:\n  Widget:\n    definition: A test term.\n")
    (specs_root / "features" / "existing.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "id": "FEAT-EXISTING", "title": "Existing Feature", "version": 1, "status": "approved",
        "summary": "Pre-existing approved feature for update-scenario tests.",
        "requirements": [{"id": "REQ-EXIST-001", "text": "Pre-existing requirement."}],
        "acceptance_criteria": [{"id": "AC-EXIST-001", "text": "Pre-existing acceptance criterion."}],
        "invariants": [],
    }, sort_keys=False))

    for name in ("requirement_analysis_service", "spec_author_service", "spec_proposal_validator",
                 "spec_review_service", "spec_lifecycle_service"):
        getattr(client.app.state, name).specs_root = specs_root
    # PlannerService (E4) keeps its own specs_root separate from its
    # context_builder/validator sub-objects -- redirect all three so
    # plan creation, requirement-id validation, and staleness checks
    # are ALL isolated to this same disposable tree.
    client.app.state.planner_service.specs_root = specs_root
    client.app.state.planner_service.context_builder.specs_root = specs_root
    client.app.state.planner_service.validator.specs_root = specs_root
    return specs_root


def register(client, repo, name="demo"):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text
    return client.get("/api/repositories").json()[0]["id"]


def new_change(client, title, description="", project_id=None):
    data = {"title": title, "description": description}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post("/api/changes", data=data).json()["id"]


def envelope(payload):
    return {"is_error": False, "subtype": "success", "result": json.dumps(payload)}


def set_fake(client, payload):
    env = envelope(payload)

    def runner(argv, cwd, timeout):
        class R:
            returncode = 0
            stdout = json.dumps(env)
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = runner


def ra_payload(**overrides):
    p = {"problem_statement": "Users need X.", "functional_requirements": ["Do X."],
         "ambiguities": []}
    p.update(overrides)
    return p


def spec_payload(**overrides):
    p = {"feature_id": "FEAT-NEW-THING", "title": "New Thing", "summary": "A new thing.",
         "scope_includes": ["the new thing"], "scope_excludes": [],
         "requirements": [{"id": "REQ-001", "text": "Must do the thing."}],
         "acceptance_criteria": [{"id": "AC-001", "text": "Doing the thing is observable."}],
         "invariants": [{"id": "INV-001", "text": "The thing never breaks an existing invariant."}]}
    p.update(overrides)
    return p


def review_payload(verdict="PASS", **overrides):
    p = {"verdict": verdict, "findings": []}
    p.update(overrides)
    return p


def do_analyze(client, cid, payload=None):
    set_fake(client, payload or ra_payload())
    r = client.post(f"/api/changes/{cid}/requirements/analyze", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def do_author(client, cid, spec=None):
    set_fake(client, spec or spec_payload())
    r = client.post(f"/api/changes/{cid}/spec-proposals", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def do_review(client, pid, verdict="PASS", **overrides):
    set_fake(client, review_payload(verdict, **overrides))
    r = client.post(f"/api/spec-proposals/{pid}/review", data={"provider": "claude"})
    assert r.status_code == 200, r.text
    return r.json()


def full_change_with_proposal(client, git_repo, spec_env, spec=None, ra=None, project=True):
    root, repo = git_repo
    rid = register(client, repo, "demo") if project else None
    cid = new_change(client, "Lifecycle test change", "A harmless test intent.", project_id=rid)
    do_analyze(client, cid, ra)
    author_result = do_author(client, cid, spec)
    return cid, author_result["proposal"]


# ================================================================ Requirement Analysis

def test_requirement_analysis_structured_result(client, git_repo, spec_env):
    root, repo = git_repo
    cid = new_change(client, "RA test change", "Do the thing.")
    result = do_analyze(client, cid, ra_payload(problem_statement="Real problem", functional_requirements=["Real req"]))
    assert result["outcome"] == "READY"
    wp = result["work_product"]
    assert wp["kind"] == "REQUIREMENT_ANALYSIS"
    content = json.loads(wp["content_metadata"])
    assert content["problem_statement"] == "Real problem"


def test_requirement_analysis_ambiguity_classification(client, git_repo, spec_env):
    cid = new_change(client, "Ambiguity test change")
    result = do_analyze(client, cid, ra_payload(ambiguities=[
        {"issue": "cache strategy", "classification": "HOW_DECISION"},
        {"issue": "delete semantics", "classification": "HUMAN_SPEC_CHANGE_REQUIRED", "question": "Hard or soft delete?", "reason": "different data outcomes"},
    ]))
    assert len(result["human_decision_ids"]) == 1  # only the WHAT-level one


def test_how_decision_never_creates_human_decision(client, git_repo, spec_env):
    cid = new_change(client, "How decision change")
    result = do_analyze(client, cid, ra_payload(ambiguities=[{"issue": "library choice", "classification": "HOW_DECISION"}]))
    assert result["human_decision_ids"] == []
    state = client.get(f"/api/changes/{cid}/workflow").status_code  # no workflow created yet, just sanity
    r = client.app.state.human_decisions.pending_for_change(cid)
    assert r is False


def test_what_decision_requires_human(client, git_repo, spec_env):
    cid = new_change(client, "What decision change")
    do_analyze(client, cid, ra_payload(ambiguities=[
        {"issue": "permission model", "classification": "HUMAN_SPEC_CHANGE_REQUIRED", "question": "Who can delete?", "reason": "changed permission semantics"}]))
    assert client.app.state.human_decisions.pending_for_change(cid) is True


def test_requirement_analysis_creates_traceable_task(client, git_repo, spec_env):
    cid = new_change(client, "RA task trace change")
    result = do_analyze(client, cid)
    task = client.get(f"/api/tasks/{result['task_id']}").json()
    assert task["task_type"] == "REQUIREMENT_ANALYSIS"
    assert task["change_id"] == cid
    outputs = client.get(f"/api/tasks/{result['task_id']}/work-products").json()["outputs"]
    assert outputs[0]["id"] == result["work_product"]["id"]


# ======================================================================= Spec Authoring

def test_valid_spec_proposal_created(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    assert proposal["feature_id"] == "FEAT-NEW-THING"
    assert proposal["base_spec_version"] is None
    assert proposal["proposed_version"] == 1
    assert proposal["status"] == "DRAFT"
    content = proposal["proposed_content"]
    assert len(content["requirements"]) == 1


def test_invalid_structured_output_rejected(client, git_repo, spec_env):
    root, repo = git_repo
    cid = new_change(client, "Bad author output change")
    do_analyze(client, cid)
    set_fake(client, {"not_a_valid_shape": True})
    r = client.post(f"/api/changes/{cid}/spec-proposals", data={"provider": "claude"})
    assert r.json()["outcome"] in ("OUTPUT_INVALID", "EXECUTION_FAILED")
    assert r.json()["proposal"] is None


def test_duplicate_id_deterministically_remapped(client, git_repo, spec_env):
    """E5.5: a proposed requirement id colliding with an EXISTING id
    under a DIFFERENT feature is remapped, never silently overwritten."""
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env, spec=spec_payload(
        requirements=[{"id": "REQ-EXIST-001", "text": "A different requirement, same id as FEAT-EXISTING's."}]))
    content = proposal["proposed_content"]
    assert content["requirements"][0]["id"] != "REQ-EXIST-001"
    assert content["requirements"][0]["id"].startswith("REQ-EXIST-001-")
    notes = json.loads(proposal["id_remap_notes"])
    assert notes and notes[0]["original"] == "REQ-EXIST-001"

    # the existing feature's own requirement must be completely untouched
    # -- read directly from the isolated spec_env tree (the HTTP
    # /api/spec/* routes are hardwired to the real production specs_root,
    # not this test's disposable one).
    from app.services.spec_registry import SpecRegistry
    existing_feature = SpecRegistry(spec_env).load().feature("FEAT-EXISTING")
    assert any(r["id"] == "REQ-EXIST-001" and r["text"] == "Pre-existing requirement." for r in existing_feature["requirements"])


def test_updating_existing_feature_reuses_its_own_ids(client, git_repo, spec_env):
    """A proposal that legitimately updates FEAT-EXISTING may reuse ITS
    OWN requirement id -- no collision, no remap."""
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env, spec=spec_payload(
        feature_id="FEAT-EXISTING", title="Existing Feature", requirements=[{"id": "REQ-EXIST-001", "text": "Refined text."}]))
    assert proposal["base_spec_version"] == 1
    assert proposal["proposed_version"] == 2
    content = proposal["proposed_content"]
    assert content["requirements"][0]["id"] == "REQ-EXIST-001"  # reused verbatim, not remapped
    assert json.loads(proposal["id_remap_notes"]) == []


def test_new_feature_starts_at_version_one(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    assert proposal["proposed_version"] == 1
    assert proposal["base_spec_version"] is None


# ==================================================================== Validation

def test_malformed_spec_rejected_by_real_spec_registry(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env, spec=spec_payload(
        acceptance_criteria=[]))  # empty acceptance -- validator's own explicit rule
    result = client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    assert result["valid"] is False


def test_duplicate_global_requirement_id_within_same_new_feature_never_reaches_registry_error(client, git_repo, spec_env):
    """Two requirements with the same id WITHIN one proposal are
    deduplicated by _resolve_id_collisions before ever reaching
    SpecRegistry -- validation still passes structurally."""
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env, spec=spec_payload(
        requirements=[{"id": "REQ-001", "text": "First."}, {"id": "REQ-001", "text": "Second, same id."}]))
    content = proposal["proposed_content"]
    ids = [r["id"] for r in content["requirements"]]
    assert len(ids) == len(set(ids))  # already deduplicated
    result = client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    assert result["valid"] is True


def test_proposal_never_ready_when_validation_invalid(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env, spec=spec_payload(acceptance_criteria=[]))
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    p = client.get(f"/api/spec-proposals/{proposal['id']}").json()
    assert p["status"] == "REJECTED"


# ======================================================================= Spec Review

def test_review_pass_marks_proposal_ready(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    review = do_review(client, proposal["id"], "PASS")
    assert review["verdict"] == "PASS"
    p = client.get(f"/api/spec-proposals/{proposal['id']}").json()
    assert p["status"] == "READY"


def test_review_needs_refinement(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    review = do_review(client, proposal["id"], "NEEDS_REFINEMENT", findings=[
        {"category": "testability", "description": "AC-001 is too vague.", "severity": "MEDIUM"}])
    assert review["verdict"] == "NEEDS_REFINEMENT"
    p = client.get(f"/api/spec-proposals/{proposal['id']}").json()
    assert p["status"] == "NEEDS_REFINEMENT"


def test_review_human_decision_required(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    review = do_review(client, proposal["id"], "HUMAN_DECISION_REQUIRED", human_decisions=[
        {"question": "Should deletes cascade?", "reason": "materially different data outcome", "spec_change_signal": "HUMAN_SPEC_CHANGE_REQUIRED"}])
    assert review["verdict"] == "HUMAN_DECISION_REQUIRED"
    assert len(review["human_decision_ids"]) == 1
    p = client.get(f"/api/spec-proposals/{proposal['id']}").json()
    assert p["status"] == "HUMAN_DECISION_REQUIRED"


def test_logical_contradiction_finding_recorded(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    review = do_review(client, proposal["id"], "NEEDS_REFINEMENT", findings=[
        {"category": "contradiction", "description": "INV-001 contradicts REQ-001's stated behavior.", "severity": "HIGH"}])
    findings = client.get(f"/api/spec-proposals/{proposal['id']}/findings").json()
    assert any(f["category"] == "contradiction" for f in findings)


def test_untestable_requirement_finding_recorded(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    do_review(client, proposal["id"], "NEEDS_REFINEMENT", findings=[
        {"category": "testability", "description": "No observable outcome defined.", "severity": "HIGH"}])
    findings = client.get(f"/api/spec-proposals/{proposal['id']}/findings").json()
    assert any(f["category"] == "testability" for f in findings)


def test_review_does_not_receive_author_rationale(client, git_repo, spec_env):
    """The critical rule, checked directly: SpecReviewService's own
    context builder never includes a 'rationale' key anywhere."""
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)

    captured = {}
    real_invoke = client.app.state.spec_review_service.invoker.invoke

    def spying_invoke(provider, prompt, schema, cwd):
        captured["prompt"] = prompt
        return real_invoke(provider, prompt, schema, cwd)
    client.app.state.spec_review_service.invoker.invoke = spying_invoke
    set_fake(client, review_payload("PASS"))
    client.post(f"/api/spec-proposals/{proposal['id']}/review", data={"provider": "claude"})
    assert '"rationale"' not in captured["prompt"]


# ===================================================================== Refinement

def test_auto_refinement_creates_new_revision_and_history(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    do_review(client, proposal["id"], "NEEDS_REFINEMENT", findings=[{"category": "testability", "description": "clarify AC-001", "severity": "LOW"}])

    set_fake(client, spec_payload(requirements=[{"id": "REQ-001", "text": "Must do the thing (refined, with explicit timeout behavior)."}]))
    r = client.post(f"/api/spec-proposals/{proposal['id']}/refine", data={"provider": "claude"})
    assert r.status_code == 200
    refined = r.json()["proposal"]
    assert refined["id"] != proposal["id"]
    assert refined["supersedes_proposal_id"] == proposal["id"]
    assert refined["refinement_round"] == 1

    old = client.get(f"/api/spec-proposals/{proposal['id']}").json()
    assert old["status"] == "SUPERSEDED"
    assert old["proposed_content"]["requirements"][0]["text"] != refined["proposed_content"]["requirements"][0]["text"]  # history preserved, not rewritten


def test_bounded_refinement_loop_never_infinite(client, git_repo, spec_env):
    root, repo = git_repo
    cid = new_change(client, "Bounded loop change", "Test bounded refinement.")
    do_analyze(client, cid)

    call_count = {"n": 0}
    real_runner_holder = {}

    def cycling_runner(argv, cwd, timeout):
        call_count["n"] += 1
        # run_lifecycle calls requirement-analysis, author/refine, and
        # review in sequence -- distinguish all three by prompt content.
        prompt = argv[argv.index("-p") + 1]
        if "INDEPENDENT SPEC REVIEWER" in prompt:
            payload = review_payload("NEEDS_REFINEMENT", findings=[{"category": "testability", "description": "still vague", "severity": "LOW"}])
        elif "REQUIREMENTS ANALYST" in prompt:
            payload = ra_payload()
        else:
            payload = spec_payload()
        class R:
            returncode = 0
            stdout = json.dumps(envelope(payload))
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = cycling_runner

    result = client.app.state.spec_lifecycle_service.run_lifecycle(cid, provider="claude", max_rounds=3)
    assert result["outcome"] == "NEEDS_REFINEMENT"
    assert result["rounds"] == 3
    # exactly 3 review calls + 3 author-ish calls (1 initial author + 2 refine) -- bounded, not infinite
    assert call_count["n"] <= 7


def test_refinement_never_weakens_invariants_or_deletes_requirements(client, git_repo, spec_env):
    """The prompt explicitly forbids it (checked here) -- ProjectFlow
    itself does not additionally enforce this at the validator level in
    E5 (would require semantic diffing, deferred), so this test asserts
    the instruction is actually present in the refine prompt."""
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    captured = {}
    real_invoke = client.app.state.spec_author_service.invoker.invoke

    def spying_invoke(provider, prompt, schema, cwd):
        captured["prompt"] = prompt
        return real_invoke(provider, prompt, schema, cwd)
    client.app.state.spec_author_service.invoker.invoke = spying_invoke
    set_fake(client, spec_payload())
    client.post(f"/api/spec-proposals/{proposal['id']}/refine", data={"provider": "claude"})
    assert "Never remove a" in captured["prompt"]


# ================================================================== Human decisions

def test_human_decision_blocks_apply(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    do_review(client, proposal["id"], "HUMAN_DECISION_REQUIRED", human_decisions=[
        {"question": "Cascade deletes?", "reason": "data outcome", "spec_change_signal": "HUMAN_SPEC_CHANGE_REQUIRED"}])
    r = client.post(f"/api/spec-proposals/{proposal['id']}/apply")
    assert r.status_code == 400


def test_human_decision_resolution_preserved_and_resumes(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    do_review(client, proposal["id"], "HUMAN_DECISION_REQUIRED", human_decisions=[
        {"question": "Cascade deletes?", "reason": "data outcome"}])
    hd = client.get(f"/api/spec-proposals/{proposal['id']}").json()["human_decisions"][0]
    resolve = client.post(f"/api/human-decisions/{hd['id']}/resolve", data={"resolution_note": "No cascade."})
    assert resolve.status_code == 200
    assert resolve.json()["resolved"] == 1
    assert resolve.json()["resolution_note"] == "No cascade."


# =========================================================================== Apply

def test_ready_proposal_writes_canonical_spec_and_real_registry_loads_it(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    do_review(client, proposal["id"], "PASS")
    r = client.post(f"/api/spec-proposals/{proposal['id']}/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feature_id"] == "FEAT-NEW-THING"

    from app.services.spec_registry import SpecRegistry
    registry = SpecRegistry(spec_env).load()
    feature = registry.feature("FEAT-NEW-THING")
    assert feature is not None
    assert feature["status"] == "approved"


def test_apply_changes_baseline(client, git_repo, spec_env):
    from app.services.spec_registry import SpecRegistry
    before = SpecRegistry(spec_env).load().baseline_digest()

    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    do_review(client, proposal["id"], "PASS")
    client.post(f"/api/spec-proposals/{proposal['id']}/apply")

    after = SpecRegistry(spec_env).load().baseline_digest()
    assert before != after


def test_failed_apply_leaves_canonical_tree_valid(client, git_repo, spec_env):
    """Simulate a post-write validation failure and confirm the
    original file content is restored, and the tree still loads."""
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    do_review(client, proposal["id"], "PASS")

    from app.services import spec_lifecycle_service as sls
    real_serialize = sls._serialize_feature_yaml

    def broken_serialize(content, version, status="approved"):
        return "not: valid: yaml: [unterminated"
    sls._serialize_feature_yaml = broken_serialize
    try:
        with pytest.raises(Exception):
            client.app.state.spec_lifecycle_service.apply_proposal(proposal["id"])
    finally:
        sls._serialize_feature_yaml = real_serialize

    from app.services.spec_registry import SpecRegistry
    registry = SpecRegistry(spec_env).load()  # must not raise -- tree still valid
    assert registry.feature("FEAT-NEW-THING") is None  # the failed write never took effect
    assert registry.feature("FEAT-EXISTING") is not None  # unrelated feature untouched


def test_cannot_apply_a_non_ready_proposal(client, git_repo, spec_env):
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env)
    r = client.post(f"/api/spec-proposals/{proposal['id']}/apply")
    assert r.status_code == 400


# =================================================================== Plan integration

def test_plan_valid_requirement_ids_resolve(client, git_repo, spec_env):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env, project=False)
    # re-register since full_change_with_proposal made its own repo already; reuse rid instead
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    do_review(client, proposal["id"], "PASS")
    client.post(f"/api/spec-proposals/{proposal['id']}/apply")

    client.app.state.planner_service.validator.specs_root = spec_env
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})

    def plan_runner(argv, cwd, timeout):
        payload = {"summary": "x", "tasks": [{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION", "requirements": ["REQ-001"]}]}
        class R:
            returncode = 0
            stdout = json.dumps(envelope(payload))
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = plan_runner
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    assert r.json()["outcome"] == "PLAN_READY", r.json()


def test_fictional_requirement_id_rejected(client, git_repo, spec_env):
    root, repo = git_repo
    cid = new_change(client, "Fictional req id change")
    client.app.state.planner_service.validator.specs_root = spec_env
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})

    def plan_runner(argv, cwd, timeout):
        payload = {"summary": "x", "tasks": [{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION", "requirements": ["REQ-DOES-NOT-EXIST"]}]}
        class R:
            returncode = 0
            stdout = json.dumps(envelope(payload))
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = plan_runner
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    assert r.json()["outcome"] == "PLAN_INVALID"
    assert any("unknown requirement id" in e for e in r.json()["validation"]["errors"])


def test_baseline_staleness_visible(client, git_repo, spec_env):
    root, repo = git_repo
    cid = new_change(client, "Staleness change")
    client.app.state.planner_service.context_builder.specs_root = spec_env
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})

    def plan_runner(argv, cwd, timeout):
        payload = {"summary": "x", "tasks": [{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION"}]}
        class R:
            returncode = 0
            stdout = json.dumps(envelope(payload))
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = plan_runner
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    plan_id = r.json()["plan"]["id"]

    before = client.get(f"/api/plans/{plan_id}/staleness").json()
    assert before["stale"] is False

    # change the spec tree
    (spec_env / "features" / "existing.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "id": "FEAT-EXISTING", "title": "Existing Feature (renamed)", "version": 2, "status": "approved",
        "requirements": [], "acceptance_criteria": [], "invariants": [],
    }, sort_keys=False))
    after = client.get(f"/api/plans/{plan_id}/staleness").json()
    assert after["stale"] is True
    assert after["reason"] == "SPEC_BASELINE_CHANGED"
    assert after["replan_recommended"] is True


def test_plan_spec_drift_when_referenced_requirement_disappears(client, git_repo, spec_env):
    root, repo = git_repo
    cid, proposal = full_change_with_proposal(client, git_repo, spec_env, project=False)
    client.app.state.spec_lifecycle_service.validate_proposal(proposal["id"])
    do_review(client, proposal["id"], "PASS")
    client.post(f"/api/spec-proposals/{proposal['id']}/apply")

    client.app.state.planner_service.validator.specs_root = spec_env
    client.app.state.planner_service.context_builder.specs_root = spec_env
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})

    def plan_runner(argv, cwd, timeout):
        payload = {"summary": "x", "tasks": [{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION", "requirements": ["REQ-001"]}]}
        class R:
            returncode = 0
            stdout = json.dumps(envelope(payload))
            stderr = ""
        return R()
    client.app.state.planner_service.invoker.runner = plan_runner
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    plan_id = r.json()["plan"]["id"]
    assert r.json()["outcome"] == "PLAN_READY"

    # remove the requirement the plan referenced -- rewrite FEAT-NEW-THING without REQ-001
    (spec_env / "features" / "feat-new-thing.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "id": "FEAT-NEW-THING", "title": "New Thing", "version": 2, "status": "approved",
        "requirements": [], "acceptance_criteria": [{"id": "AC-001", "text": "x"}], "invariants": [],
    }, sort_keys=False))
    staleness = client.get(f"/api/plans/{plan_id}/staleness").json()
    assert staleness["reason"] == "PLAN_SPEC_DRIFT"


# ================================================================= Backward compatibility

def test_existing_manually_authored_specs_unaffected(client, spec_env):
    from app.services.spec_registry import SpecRegistry
    registry = SpecRegistry(spec_env).load()
    assert registry.feature("FEAT-EXISTING")["version"] == 1


def test_spec_gate_unaffected_by_e5(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "Spec gate unaffected", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Spec gate unaffected"][0]
    assert client.get(f"/api/tasks/{tid}/spec-gate").json()["outcome"] == "NOT_APPLICABLE"


def test_planner_still_works_without_any_spec_proposal(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Planner-only change")
    client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "VIBE"})
    set_fake(client, {"summary": "x", "tasks": [{"key": "T1", "title": "Build", "task_type": "IMPLEMENTATION"}]})
    r = client.post(f"/api/changes/{cid}/plan", data={"provider": "claude"})
    assert r.json()["outcome"] == "PLAN_READY"


def test_manual_task_and_supervisor_unaffected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1
    r = client.post("/api/tasks", data={"title": "Manual task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Manual task"][0]
    client.post(f"/api/tasks/{tid}/select")
    w = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    w = [x for x in client.get(f"/api/tasks/{tid}").json()["workspaces"] if x["agent"] == "codex"][-1]
    r2 = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    assert r2.status_code == 303


def test_production_specs_root_never_touched_by_this_suite():
    """Meta-test: confirms the isolation fixture is doing its job -- the
    real repo's specs/features/ directory must contain exactly its
    known, committed files after this whole test module runs."""
    from pathlib import Path
    real_specs = Path(__file__).resolve().parent.parent / "specs" / "features"
    names = {p.name for p in real_specs.glob("*.yaml")}
    assert "feat-new-thing.yaml" not in names
    assert "feat-workflow-profile-api.yaml" not in names


def test_real_spec_lifecycle_end_to_end(client, git_repo, spec_env):
    """E5.24: one safe, real, non-fake Spec Lifecycle run against a
    disposable Change and an isolated tmp_path specs_root (spec_env) --
    NO fake runner is installed anywhere in this test, so every
    Requirement Analysis / Spec Author / Spec Review invocation below is
    a genuine `claude -p --json-schema ... --tools "" --max-turns 1`
    subprocess call. Drives Requirement Analysis -> Spec Author ->
    independent Spec Review -> bounded auto-refinement (if the reviewer
    asks for it) -> Apply (only into the disposable spec_env tree, never
    the real repo's specs/), printing every artifact captured along the
    way per the phase report's REAL SPEC TEST section. Confirms the
    real production specs/features/ tree is untouched throughout."""
    from pathlib import Path
    real_specs_before = {p.name for p in (Path(__file__).resolve().parent.parent / "specs" / "features").glob("*.yaml")}

    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(
        client,
        "Expose the current Workflow Profile for a Change through a read-only API",
        description="Add a safe, read-only endpoint that returns the WorkflowProfile "
                     "(profile_key, current_stage, unmet_gates) for an existing Change, "
                     "so external tooling can observe workflow state without mutating it.",
        project_id=rid,
    )

    # -- Requirement Analysis (real invocation) --
    ra = client.post(f"/api/changes/{cid}/requirements/analyze", data={"provider": "claude"})
    assert ra.status_code == 200, ra.text
    ra_body = ra.json()
    print("REAL SPEC TEST -- requirement analysis outcome:", ra_body["outcome"])
    assert ra_body["outcome"] == "READY", ra_body
    ra_wp = client.get(f"/api/changes/{cid}/requirements").json()
    ra_meta = ra_wp["content_metadata"]
    print("REAL SPEC TEST -- functional requirements:", len(ra_meta.get("functional_requirements", [])))
    print("REAL SPEC TEST -- ambiguities:", len(ra_meta.get("ambiguities", [])))
    print("REAL SPEC TEST -- human decisions from analysis:", len(ra_body.get("human_decision_ids", [])))

    # -- Spec Author (real invocation, separate process from analysis) --
    prop = client.post(f"/api/changes/{cid}/spec-proposals", data={"provider": "claude"})
    assert prop.status_code == 200, prop.text
    prop_body = prop.json()
    print("REAL SPEC TEST -- spec author outcome:", prop_body["outcome"])
    assert prop_body["outcome"] == "READY", prop_body
    proposal = prop_body["proposal"]
    pid = proposal["id"]
    content = proposal["proposed_content"]
    print("REAL SPEC TEST -- proposal id:", pid, "feature_id:", content["feature_id"], "revision:", proposal["proposed_version"])
    print("REAL SPEC TEST -- requirements:", len(content.get("requirements", [])))
    print("REAL SPEC TEST -- acceptance criteria:", len(content.get("acceptance_criteria", [])))
    print("REAL SPEC TEST -- invariants:", len(content.get("invariants", [])))
    print("REAL SPEC TEST -- validation result:", json.dumps(proposal["validation_result"]))

    baseline_before = None
    try:
        from app.services.spec_registry import SpecRegistry
        baseline_before = SpecRegistry(spec_env).load().baseline_digest()
    except Exception as exc:  # empty tree is fine pre-apply
        print("REAL SPEC TEST -- baseline before (unloadable, expected if tree still empty of this feature):", exc)
    print("REAL SPEC TEST -- baseline before:", baseline_before)

    # -- Independent Spec Review (real invocation, separate process, no
    #    author rationale in its context -- see SpecReviewService.review) --
    refinement_rounds = 0
    review = client.post(f"/api/spec-proposals/{pid}/review", data={"provider": "claude"})
    assert review.status_code == 200, review.text
    review_body = review.json()
    verdict = review_body.get("verdict")
    print("REAL SPEC TEST -- review outcome:", review_body["outcome"], "verdict:", verdict)
    findings = client.get(f"/api/spec-proposals/{pid}/findings").json()
    for f in findings:
        print("REAL SPEC TEST -- finding:", f.get("category"), "|", f.get("severity"), "|", f.get("description"))

    # -- Bounded auto-refinement if the independent reviewer asked for it --
    while verdict == "NEEDS_REFINEMENT" and refinement_rounds < 3:
        refinement_rounds += 1
        ref = client.post(f"/api/spec-proposals/{pid}/refine", data={"provider": "claude"})
        assert ref.status_code == 200, ref.text
        ref_body = ref.json()
        print(f"REAL SPEC TEST -- refinement round {refinement_rounds} outcome:", ref_body["outcome"])
        assert ref_body["outcome"] == "READY", ref_body
        pid = ref_body["proposal"]["id"]
        review = client.post(f"/api/spec-proposals/{pid}/review", data={"provider": "claude"})
        assert review.status_code == 200, review.text
        review_body = review.json()
        verdict = review_body.get("verdict")
        print(f"REAL SPEC TEST -- review after round {refinement_rounds} verdict:", verdict)
    print("REAL SPEC TEST -- total refinement rounds:", refinement_rounds)

    human_decisions = client.get(f"/api/spec-proposals/{pid}").json()["human_decisions"]
    print("REAL SPEC TEST -- human decisions on final proposal:", len(human_decisions))
    for hd in human_decisions:
        print("REAL SPEC TEST -- human decision:", hd["question"], "|", hd["spec_change_signal"])

    if verdict == "PASS" and not human_decisions:
        apply_r = client.post(f"/api/spec-proposals/{pid}/apply")
        assert apply_r.status_code == 200, apply_r.text
        apply_body = apply_r.json()
        print("REAL SPEC TEST -- apply outcome:", json.dumps({k: v for k, v in apply_body.items() if k != "content"}))
        from app.services.spec_registry import SpecRegistry
        baseline_after = SpecRegistry(spec_env).load().baseline_digest()
        print("REAL SPEC TEST -- baseline after:", baseline_after)
        assert baseline_after != baseline_before
    else:
        print("REAL SPEC TEST -- apply skipped: verdict", verdict, "or unresolved human decisions -- correctly withheld from an unready proposal (spec_env stays disposable regardless)")

    real_specs_after = {p.name for p in (Path(__file__).resolve().parent.parent / "specs" / "features").glob("*.yaml")}
    assert real_specs_after == real_specs_before, "REAL SPEC TEST must never touch the production specs/ tree"
    print("REAL SPEC TEST -- confirmed: production specs/features/ untouched, still exactly", sorted(real_specs_before))
