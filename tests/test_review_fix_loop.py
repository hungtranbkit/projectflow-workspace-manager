"""Independent Code Review, Security Review & Autonomous Fix Loop
(Phase E9). Every test here uses a FAKE invoker.runner (same envelope-
override pattern E5-E7's own review tests already established) -- the
one real, non-fake proof lives in tests/test_review_fix_loop_real.py,
matching this repo's own real-vs-fake test-file convention."""
from __future__ import annotations
import json
import subprocess

import pytest

from tests.test_autonomous_execution import register, new_change, materialize_task
from tests.test_worktree_manager import _select_and_create_workspace


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
    client.app.state.code_review_service.invoker.runner = runner


PASS = {"verdict": "PASS", "findings": [], "summary": "looks good"}


def _committed_task(client, cid, title="Commit for review", scope_hints=None):
    """A real Task with a real managed worktree that has a real commit
    beyond base_commit -- the minimum a CodeReviewService.review_task()
    call needs (an actual diff to review)."""
    tid, _ = materialize_task(client, cid, title=title, scope_hints=scope_hints)
    rid = client.app.state.db.one("SELECT project_id FROM changes WHERE id=?", (cid,))["project_id"]
    repo_row = client.app.state.db.one("SELECT * FROM repositories WHERE id=?", (rid,))
    w = _select_and_create_workspace(client, tid, rid)
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "feature.py").write_text("def feature():\n    return 1\n")
    subprocess.run(["git", "add", "feature.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "implement feature"], cwd=worktree, check=True, capture_output=True)
    return tid, w


# ================================================================ Review output validation (E9.10/E9.37)

def test_code_review_pass(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Review pass change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, PASS)
    r = client.post(f"/api/tasks/{tid}/review/code")
    body = r.json()
    assert body["outcome"] == "REVIEWED", body
    assert body["verdict"] == "PASS"


def test_code_review_pass_with_findings(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Review pass with findings change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "PASS_WITH_FINDINGS", "findings": [
        {"category": "MAINTAINABILITY", "severity": "LOW", "title": "minor duplication", "description": "x"}]})
    body = client.post(f"/api/tasks/{tid}/review/code").json()
    assert body["verdict"] == "PASS_WITH_FINDINGS"
    findings = client.get(f"/api/tasks/{tid}/findings").json()
    assert len(findings) == 1 and findings[0]["severity"] == "LOW"


def test_code_review_fix_required(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Review fix required change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "FIX_REQUIRED", "findings": [
        {"category": "CORRECTNESS", "severity": "HIGH", "title": "missing edge case", "description": "negative input not handled"}]})
    body = client.post(f"/api/tasks/{tid}/review/code").json()
    assert body["verdict"] == "FIX_REQUIRED"
    findings = client.get(f"/api/tasks/{tid}/findings").json()
    assert findings[0]["severity"] == "HIGH"


def test_code_review_human_decision_required(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Review HD change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "HUMAN_DECISION_REQUIRED", "findings": [],
                       "human_decisions": [{"question": "Should deletes be soft or hard?", "reason": "ambiguous"}]})
    body = client.post(f"/api/tasks/{tid}/review/code").json()
    assert body["verdict"] == "HUMAN_DECISION_REQUIRED"
    assert len(body["human_decision_ids"]) == 1
    change_id = client.app.state.db.one("SELECT change_id FROM tasks WHERE id=?", (tid,))["change_id"]
    assert client.app.state.human_decisions.pending_for_change(change_id) is True


def test_code_review_reject(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Review reject change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "REJECT", "findings": [], "summary": "fundamentally wrong approach"})
    body = client.post(f"/api/tasks/{tid}/review/code").json()
    assert body["verdict"] == "REJECT"


def test_invalid_pass_with_critical_finding_rejected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Invalid review change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "PASS", "findings": [
        {"category": "SECURITY", "severity": "CRITICAL", "title": "sql injection", "description": "x"}]})
    body = client.post(f"/api/tasks/{tid}/review/code").json()
    assert body["outcome"] == "REVIEW_OUTPUT_INVALID", body
    assert not client.get(f"/api/tasks/{tid}/reviews").json()


# ================================================================ Finding persistence (E9.11/E9.37)

def test_finding_deduplicated_across_rounds(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Dedup change", project_id=rid)
    tid, w = _committed_task(client, cid)
    finding = {"category": "CORRECTNESS", "severity": "HIGH", "title": "same issue", "description": "x"}
    set_fake(client, {"verdict": "FIX_REQUIRED", "findings": [finding]})
    client.post(f"/api/tasks/{tid}/review/code")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "feature.py").write_text("def feature():\n    return 2\n")
    subprocess.run(["git", "add", "feature.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "tweak"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/tasks/{tid}/review/code")  # same finding re-reported
    findings = client.get(f"/api/tasks/{tid}/findings").json()
    # The round-1 finding is auto-resolved (SUPERSEDED) at the new head
    # commit, then re-raised as a fresh OPEN row with the SAME
    # fingerprint since the reviewer still reports it -- never two
    # simultaneously-OPEN rows for the identical unresolved issue.
    assert len(findings) == 2
    assert sorted(f["status"] for f in findings) == ["OPEN", "SUPERSEDED"]
    assert findings[0]["fingerprint"] == findings[1]["fingerprint"]
    assert len(client.app.state.findings_store.open_blocking(tid)) == 1


def test_finding_resolve_requires_reference(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Resolve change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "FIX_REQUIRED", "findings": [
        {"category": "CORRECTNESS", "severity": "HIGH", "title": "bug", "description": "x"}]})
    client.post(f"/api/tasks/{tid}/review/code")
    fid = client.get(f"/api/tasks/{tid}/findings").json()[0]["id"]
    with pytest.raises(Exception):
        client.app.state.findings_store.resolve(fid, "")
    r2 = client.post(f"/api/findings/{fid}/resolve", data={"resolution_reference": "fixed in abc123"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "RESOLVED"


# ================================================================ CodeReview context (E9.4/E9.19)

def test_code_review_uses_real_diff_not_builder_reasoning(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Real diff change", project_id=rid)
    tid, w = _committed_task(client, cid)
    captured = {}

    def runner(argv, cwd, timeout):
        captured["prompt"] = argv[2]
        class R:
            returncode = 0
            stdout = json.dumps(envelope(PASS))
            stderr = ""
        return R()
    client.app.state.code_review_service.invoker.runner = runner
    client.post(f"/api/tasks/{tid}/review/code")
    assert "def feature():" in captured["prompt"]  # the real diff content, verbatim
    assert "feature.py" in captured["prompt"]
    # the ONLY thing this invocation ever received is this module's own
    # prompt text -- a brand-new, tool-less, --max-turns 1 subprocess
    # has no session/conversation continuity with the Builder's own
    # interactive session by construction (PlannerAgentInvoker's own
    # docstring), so there is nothing further to assert here beyond
    # "the real diff is present, the prompt is exactly what was sent."


def test_no_managed_worktree_no_changes(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "No worktree change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    body = client.post(f"/api/tasks/{tid}/review/code").json()
    assert body["outcome"] == "NO_MANAGED_WORKTREE"


# ================================================================ Security applicability (E9.7/E9.37)

def test_security_review_not_applicable_for_trivial_change(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Trivial change", project_id=rid)
    tid, w = _committed_task(client, cid)
    ws = client.app.state.worktree_manager.get_task_worktree(tid)
    t = client.app.state.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    applicability = client.app.state.security_applicability_service.applicable(tid, ws, None, None)
    assert applicability["outcome"] == "SECURITY_REVIEW_NOT_APPLICABLE"


def test_security_review_required_for_auth_path_change(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Auth change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "auth_login.py").write_text("def login(): pass\n")
    subprocess.run(["git", "add", "auth_login.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add login"], cwd=worktree, check=True, capture_output=True)
    ws = client.app.state.worktree_manager.get_task_worktree(tid)
    applicability = client.app.state.security_applicability_service.applicable(tid, ws, None, None)
    assert applicability["outcome"] == "SECURITY_REVIEW_REQUIRED"
    assert applicability["reasons"]


def test_security_review_required_by_controlled_profile(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Controlled change", project_id=rid)
    tid, w = _committed_task(client, cid)
    ws = client.app.state.worktree_manager.get_task_worktree(tid)
    applicability = client.app.state.security_applicability_service.applicable(tid, ws, "CONTROLLED", None)
    assert applicability["outcome"] == "SECURITY_REVIEW_REQUIRED"


# ================================================================ SecurityReview dedicated evidence (E9.8/E9.24/E9.37)

def test_security_review_produces_dedicated_work_product(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Security WP change", project_id=rid)
    tid, w = _committed_task(client, cid)
    client.app.state.security_review_service.invoker.runner = client.app.state.code_review_service.invoker.runner
    set_fake(client, {"verdict": "PASS", "findings": []})
    body = client.post(f"/api/tasks/{tid}/review/security")
    body = body.json()
    assert body["outcome"] == "REVIEWED"
    wp = client.app.state.db.one("SELECT * FROM work_products WHERE id=?", (body["work_product_id"],))
    assert wp["kind"] == "SECURITY_REVIEW"
    review = client.app.state.db.one("SELECT * FROM review_runs WHERE id=?", (body["review_id"],))
    assert review["review_kind"] == "SECURITY"


def test_security_pass_no_longer_aliases_review_pass(client, git_repo):
    """E9.24's own critical fix: once review_gate is wired (it always
    is in the real app), a Change with CodeReview PASS but NO
    SecurityReview at all, in a context where security IS applicable,
    must NOT show SECURITY_PASS as satisfied merely because
    REVIEW_PASS is."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Security gate change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "auth_login.py").write_text("def login(): pass\n")
    subprocess.run(["git", "add", "auth_login.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add login"], cwd=worktree, check=True, capture_output=True)
    orch = client.app.state.review_fix_orchestrator
    assert orch.security_pass(tid) is False  # applicable, but no SecurityReview evidence at all


def test_finding_auto_resolved_when_no_longer_reported_after_fix(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Auto resolve change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "FIX_REQUIRED", "findings": [
        {"category": "CORRECTNESS", "severity": "HIGH", "title": "missing case", "description": "x"}]})
    client.post(f"/api/tasks/{tid}/review/code")
    fid = client.get(f"/api/tasks/{tid}/findings").json()[0]["id"]
    assert client.app.state.db.one("SELECT status FROM findings WHERE id=?", (fid,))["status"] == "OPEN"

    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "feature.py").write_text("def feature():\n    return 42\n")
    subprocess.run(["git", "add", "feature.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix the case"], cwd=worktree, check=True, capture_output=True)
    set_fake(client, PASS)  # reviewer no longer reports the issue
    client.post(f"/api/tasks/{tid}/review/code")
    assert client.app.state.db.one("SELECT status FROM findings WHERE id=?", (fid,))["status"] == "SUPERSEDED"
    assert client.app.state.findings_store.open_blocking(tid) == []


# ================================================================ Fix loop (E9.12-E9.18/E9.37)

def test_fix_required_creates_fix_task_transfers_worktree(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Fix loop change", project_id=rid)
    tid, w = _committed_task(client, cid, scope_hints=["feature.py"])
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, {"verdict": "FIX_REQUIRED", "findings": [
        {"category": "CORRECTNESS", "severity": "HIGH", "title": "wrong return value", "description": "should return 42"}]})
    result = client.app.state.review_fix_orchestrator.tick(tid)
    assert result["outcome"] == "CODE_REVIEW_RUN"
    result2 = client.app.state.review_fix_orchestrator.tick(tid)
    assert result2["outcome"] in ("FIX_BUILDER_LAUNCHED", "EXECUTION_FAILED", "FIX_BUILDER_ALREADY_RUNNING"), result2
    fix_task_id = result2["fix_task_id"]
    fix_task = client.app.state.db.one("SELECT * FROM tasks WHERE id=?", (fix_task_id,))
    assert fix_task["fix_of_task_id"] == tid
    assert fix_task["task_type"] == "FIX"
    ws_now = client.app.state.db.one("SELECT * FROM agent_workspaces WHERE id=?", (w["id"],))
    assert ws_now["task_id"] == fix_task_id  # ownership transferred, same branch/worktree_path
    assert ws_now["branch"] == w["branch"]
    assert ws_now["worktree_path"] == w["worktree_path"]


def test_bounded_at_three_rounds(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Bounded loop change", project_id=rid)
    tid, w = _committed_task(client, cid)
    db = client.app.state.db
    ws = db.one("SELECT * FROM agent_workspaces WHERE id=?", (w["id"],))
    # round_number=3 (the 4th CODE review) means 3 fix rounds (0,1,2)
    # already happened -- MAX_ROUNDS=3 blocks starting a 4th.
    db.execute(
        "INSERT INTO review_runs(task_id,workspace_id,reviewer_type,reviewer_agent,reviewed_commit,status,findings,"
        "completed_at,review_kind,verdict,provider,base_commit,worktree_id,round_number) "
        "VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,?,?,?,?,?)",
        (tid, w["id"], "CODE_REVIEW_AI", "claude", client.app.state.git.head(w["worktree_path"]), "FIX_REQUIRED", "",
         "CODE", "FIX_REQUIRED", "claude", ws["base_commit"], w["id"], 3))
    result = client.app.state.review_fix_orchestrator.tick(tid)
    assert result["outcome"] == "REVIEW_FIX_LIMIT_REACHED", result


def test_finding_from_original_task_resolved_after_fix_task_commit(client, git_repo):
    """Real bug caught by this phase's own real Fix-loop test: a
    Finding created under the ORIGINAL Task's id must still be
    resolvable once ownership transfers to a Fix Task (a different
    task_id) -- findings_store/review lookups must span the whole
    fix-chain (task_chain_ids), never a single task_id in isolation."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Chain-aware resolve change", project_id=rid)
    tid, w = _committed_task(client, cid, scope_hints=["feature.py"])
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, {"verdict": "FIX_REQUIRED", "findings": [
        {"category": "CORRECTNESS", "severity": "HIGH", "title": "wrong value", "description": "x"}]})
    orch = client.app.state.review_fix_orchestrator
    orch.tick(tid)  # code review under the ORIGINAL task_id
    original_finding = client.get(f"/api/tasks/{tid}/findings").json()[0]
    assert original_finding["status"] == "OPEN"
    assert original_finding["task_id"] == tid

    tick2 = orch.tick(tid)  # creates the Fix Task, transfers the worktree
    fix_task_id = tick2["fix_task_id"]
    assert fix_task_id != tid

    # A real commit + PASS from the (simulated) Fix Builder, then re-review.
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "feature.py").write_text("def feature():\n    return 42\n")
    subprocess.run(["git", "add", "feature.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix"], cwd=worktree, check=True, capture_output=True)
    set_fake(client, PASS)
    tick3 = orch.tick(fix_task_id)
    assert tick3["outcome"] == "CODE_REVIEW_RUN", tick3

    # The finding created under the ORIGINAL task_id is now resolved --
    # visible/queryable from EITHER task_id in the chain.
    row = client.app.state.db.one("SELECT * FROM findings WHERE id=?", (original_finding["id"],))
    assert row["status"] != "OPEN", row
    assert client.get(f"/api/tasks/{tid}/findings").json()
    assert client.get(f"/api/tasks/{fix_task_id}/findings").json()
    assert not client.app.state.findings_store.open_blocking(tid)
    assert not client.app.state.findings_store.open_blocking(fix_task_id)


# ================================================================ Human escalation (E9.21/E9.37)

def test_human_decision_stops_loop(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "HD stop change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "HUMAN_DECISION_REQUIRED", "findings": [],
                       "human_decisions": [{"question": "What should happen on conflict?", "reason": "ambiguous"}]})
    client.app.state.review_fix_orchestrator.tick(tid)
    result = client.app.state.review_fix_orchestrator.tick(tid)
    assert result["outcome"] == "WAITING_HUMAN", result


# ================================================================ Security critical stop (E9.22)

def test_security_critical_finding_blocks(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Critical security change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "auth_login.py").write_text("def login(): pass\n")
    subprocess.run(["git", "add", "auth_login.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add login"], cwd=worktree, check=True, capture_output=True)

    def runner(argv, cwd, timeout):
        prompt = argv[2]
        # SECURITY_REVIEWER_PREAMBLE's own distinctive text -- CODE_
        # REVIEWER_PREAMBLE also mentions "SECURITY SIGNALS" in passing,
        # so a bare "SECURITY" substring check would misfire on the
        # CODE review invocation too.
        if "INDEPENDENT security reviewer" in prompt:
            payload = {"verdict": "FIX_REQUIRED", "findings": [
                {"category": "SECURITY", "severity": "CRITICAL", "title": "no auth check", "description": "x"}]}
        else:
            payload = PASS
        class R:
            returncode = 0
            stdout = json.dumps(envelope(payload))
            stderr = ""
        return R()
    client.app.state.code_review_service.invoker.runner = runner
    client.app.state.security_review_service.invoker.runner = runner

    client.app.state.review_fix_orchestrator.tick(tid)  # code review runs
    result = client.app.state.review_fix_orchestrator.tick(tid)  # security review runs
    assert result["outcome"] == "SECURITY_CRITICAL_BLOCK", result
    change_id = client.app.state.db.one("SELECT change_id FROM tasks WHERE id=?", (tid,))["change_id"]
    assert client.app.state.human_decisions.pending_for_change(change_id) is True


# ================================================================ TaskDecision / gates (E9.23)

def test_review_pass_none_falls_back_to_legacy_when_no_e9_review(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Legacy fallback change", project_id=rid)
    tid, w = _committed_task(client, cid)
    orch = client.app.state.review_fix_orchestrator
    assert orch.review_pass(tid) is None  # no E9 evidence yet -- caller falls back


# ================================================================ Review staleness (E9.28)

def test_review_stale_after_new_commit(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Staleness change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{tid}/review/code")
    orch = client.app.state.review_fix_orchestrator
    assert orch.review_pass(tid) is True
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "feature.py").write_text("def feature():\n    return 3\n")
    subprocess.run(["git", "add", "feature.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "another change"], cwd=worktree, check=True, capture_output=True)
    assert orch.review_pass(tid) is False  # REVIEW_STALE


# ================================================================ Integration readiness (E9.30)

def test_integration_readiness_ready(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Integration ready change", project_id=rid)
    tid, w = _committed_task(client, cid)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{tid}/review/code")
    result = client.app.state.review_fix_orchestrator.integration_readiness(tid)
    assert result["outcome"] == "INTEGRATION_READY", result


def test_integration_readiness_blocked_by_conflict(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Integration conflict change", project_id=rid)
    tid, w = _committed_task(client, cid)
    (repo / "feature.py").write_text("def feature():\n    return 999\n")
    subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "main diverges"], cwd=repo, check=True, capture_output=True)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{tid}/review/code")
    result = client.app.state.review_fix_orchestrator.integration_readiness(tid)
    assert "INTEGRATION_CONFLICT" in result["blockers"], result


# ================================================================ Worktree isolation (E9)

def test_review_never_touches_canonical_checkout(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Review isolation change", project_id=rid)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    tid, w = _committed_task(client, cid)
    set_fake(client, {"verdict": "FIX_REQUIRED", "findings": [
        {"category": "CORRECTNESS", "severity": "HIGH", "title": "x", "description": "x"}]})
    client.post(f"/api/tasks/{tid}/review/code")
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert after == before


# ================================================================ Backward compatibility (E9.37)

def test_legacy_manual_review_runs_unaffected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "Legacy manual review task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Legacy manual review task"][0]
    w = _select_and_create_workspace(client, tid, rid, agent="codex")
    r0 = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    assert r0.status_code == 303, r0.text
    r = client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "human-tester"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    r2 = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS", "notes": "looks fine"}, follow_redirects=False)
    assert r2.status_code == 303, r2.text
    run = client.app.state.db.one("SELECT * FROM review_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
    assert run["status"] == "PASS"
    assert run["review_kind"] is None  # legacy row, untouched by E9's new columns


def test_e8_autonomous_readiness_unaffected(client, git_repo):
    from tests.test_autonomous_execution import enable_autonomous
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "E8 unaffected by E9 change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "AUTO_READY"


def test_security_not_applicable_for_empty_diff_even_under_controlled(client, git_repo):
    """Real regression caught by this phase's own full suite: a Task
    whose managed worktree has genuinely nothing changed yet has
    nothing for CONTROLLED's own stricter security-review default to
    apply to -- CodeReviewService.review_task() itself would return
    NO_CHANGES for the same diff, so applicability must agree."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Empty diff controlled change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    ws = client.app.state.worktree_manager.get_task_worktree(tid)
    applicability = client.app.state.security_applicability_service.applicable(tid, ws, "CONTROLLED", None)
    assert applicability["outcome"] == "SECURITY_REVIEW_NOT_APPLICABLE", applicability
    orch = client.app.state.review_fix_orchestrator
    assert orch.security_pass(tid) is True
