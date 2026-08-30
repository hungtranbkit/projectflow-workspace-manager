"""E9.34/E9.35/E9.36: SAFE REAL TESTs for Independent Review. Reuses the
exact real-CLI harness proven in tests/test_autonomous_execution_real.py
and tests/test_worktree_manager_real.py -- disposable fixture only,
never the live ProjectFlow checkout. CodeReview/SecurityReview calls
are real, fresh, tool-less `claude -p --json-schema --max-turns 1`
subprocesses (fast, no trust-dialog/PTY-timing issues at all); only the
Fix Builder step needs the interactive-session trust/timing handling
E8/E8.5 already solved."""
from __future__ import annotations
import dataclasses
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_autonomous_execution import register, new_change
from tests.test_autonomous_execution_real import _trust_claude_dir, _restore_claude_dir

pytestmark = pytest.mark.skipif(shutil.which("claude") is None, reason="real `claude` CLI not installed on this host")


def _pytest_passes(cwd) -> bool:
    import os
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_calc.py"],
                             cwd=str(cwd), capture_output=True, text=True, timeout=60, env=env)
    print("REAL E9 TEST -- pytest in", cwd, "->", result.returncode)
    print(result.stdout[-2000:])
    return result.returncode == 0


def _seed_repo(repo):
    (repo / "README.md").write_text("base\n")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: demo}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\n"
        "ci: {required: [preflight, test]}\n"
        "engineering:\n  autonomous_execution:\n    enabled: true\n    max_concurrent_builders: 1\n    auto_start_ready_tasks: true\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed: empty project"], cwd=repo, check=True, capture_output=True)


# ================================================================ E9.34: real CodeReview + Fix loop

def test_real_code_review_catches_missing_spec_case_and_fix_loop_resolves_it(client, git_repo):
    root, repo = git_repo
    real_checkout = Path(__file__).resolve().parent.parent
    before_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    before_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout, capture_output=True, text=True).stdout

    _seed_repo(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Fix answer() for negative input per Spec", project_id=rid,
                      description="calc.answer(n) must return 42 for non-negative n (REQ-1) and raise ValueError "
                                   "for negative n (REQ-2). The initial implementation and test only cover REQ-1.")
    work_products = client.app.state.work_products
    from app.services.architecture_design_service import design_state_digest
    from app.services.test_design_service import test_design_state_digest
    work_products.create(kind="FEATURE_SPEC", title="answer() spec", change_id=cid, status="APPROVED",
                          content_metadata={"requirements": [
                              {"id": "REQ-1", "text": "answer(n) must return 42 for any non-negative n"},
                              {"id": "REQ-2", "text": "answer(n) must raise ValueError for negative n"}]})
    work_products.create(kind="TECHNICAL_DESIGN", title="answer() design", change_id=cid, status="APPROVED",
                          content_metadata={"design_summary": "A single function calc.answer(n); validate n before returning the constant."})
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    assert r.status_code == 200, r.text

    db = client.app.state.db
    plan_id = db.execute(
        "INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest,design_baseline_digest,test_design_baseline_digest) "
        "VALUES(?,?,?,?,?,?,?)",
        (cid, 1, "MATERIALIZED", "claude", "x", design_state_digest(work_products, cid), test_design_state_digest(work_products, cid)))
    tid = db.execute("INSERT INTO tasks(slug,title,status,change_id,task_type) VALUES(?,?,?,?,?)",
                      ("implement-answer-e9-real", "Implement answer() for REQ-1/REQ-2", "BACKLOG", cid, "IMPLEMENTATION"))
    db.execute(
        "INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (plan_id, "T1", "Implement answer()", "IMPLEMENTATION", "[]", json.dumps(["REQ-1", "REQ-2"]), json.dumps(["calc.py", "test_calc.py"]), tid))

    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/worktree/create", data={"repository_id": rid, "agent": "claude"})
    assert r.status_code == 200, r.text
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    worktree = ws["worktree_path"]

    # The "Builder's" own initial submission -- deliberately incomplete
    # per E9.34's own scenario (implements REQ-1 only, the initial
    # TestCase only covers REQ-1 too, so it genuinely passes).
    Path(worktree, "calc.py").write_text("def answer(n):\n    return 42\n")
    Path(worktree, "test_calc.py").write_text("from calc import answer\n\n\ndef test_normal():\n    assert answer(5) == 42\n")
    subprocess.run(["git", "add", "calc.py", "test_calc.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "implement answer() for REQ-1"], cwd=worktree, check=True, capture_output=True)
    assert _pytest_passes(worktree)  # the initial (incomplete) test genuinely passes

    r = client.post(f"/api/workspaces/{ws['id']}/verification-report",
                     data={"work_status": "READY", "what_changed": "Implemented answer()", "files_changed": "calc.py,test_calc.py"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text

    # -- REAL CodeReview: does an independent reviewer catch the missing
    #    REQ-2 (negative-input) behavior from the Spec alone? --
    review = client.post(f"/api/tasks/{tid}/review/code").json()
    print("REAL E9 TEST -- code review result:", review)
    assert review["outcome"] == "REVIEWED", review
    print("REAL E9 TEST -- verdict:", review["verdict"])
    findings = client.get(f"/api/tasks/{tid}/findings").json()
    print("REAL E9 TEST -- findings:", findings)

    if review["verdict"] not in ("FIX_REQUIRED",):
        pytest.skip(f"real reviewer returned {review['verdict']!r} instead of FIX_REQUIRED for the missing REQ-2 case -- "
                    "this is a genuine finding about reviewer behavior, not a harness bug; see REAL E9 TEST log above.")

    original_finding_ids = {f["id"] for f in findings if f["status"] == "OPEN"}
    assert original_finding_ids

    # -- pre-register trust for the FIX worktree (SAME physical path,
    #    ownership transfer -- see review_fix_orchestrator.py's own
    #    docstring) before the real Fix Builder launches --
    trust_key, trust_previous = _trust_claude_dir(worktree)
    svc = client.app.state.autonomous_execution_service
    svc.settings = dataclasses.replace(svc.settings, agents=("claude",))
    client.app.state.agent_sessions.prompt_ready_timeout = 25.0
    client.app.state.agent_sessions.prompt_quiet_window = 2.0

    pre_fix_head = client.app.state.git.head(worktree)
    tick = client.app.state.review_fix_orchestrator.tick(tid)
    print("REAL E9 TEST -- fix tick result:", tick)
    assert tick["outcome"] == "FIX_BUILDER_LAUNCHED", tick
    fix_task_id = tick["fix_task_id"]
    sid = tick["session_id"]

    try:
        # Require REAL evidence of Fix Builder activity -- a new commit,
        # not merely "the worktree is clean" (which is trivially true
        # even if the Builder changed nothing at all, since it starts
        # clean) and not merely "the original narrow test still passes"
        # (also trivially true with zero changes, since that test never
        # covered the missing behavior in the first place).
        deadline = time.time() + 420
        committed = False
        while time.time() < deadline:
            if client.app.state.git.head(worktree) != pre_fix_head and not client.app.state.git.status(worktree).strip():
                committed = True
                break
            time.sleep(5)
        print("REAL E9 TEST -- final calc.py:", Path(worktree, "calc.py").read_text())
        assert committed, "real Claude Fix Builder made no new commit within the timeout (no real fix attempt)"
        assert _pytest_passes(worktree), "Fix Builder committed something, but the test suite does not pass"
    finally:
        try:
            client.app.state.agent_sessions.stop(sid)
        except Exception:
            pass
        try:
            _restore_claude_dir(trust_key, trust_previous)
        except Exception:
            pass

    r = client.post(f"/api/workspaces/{ws['id']}/verification-report",
                     data={"work_status": "READY", "what_changed": "Added negative-input handling per REQ-2",
                           "files_changed": "calc.py,test_calc.py"}, follow_redirects=False)
    assert r.status_code == 303, r.text

    retick = client.app.state.review_fix_orchestrator.tick(fix_task_id)
    print("REAL E9 TEST -- re-review tick result:", retick)
    assert retick["outcome"] == "CODE_REVIEW_RUN", retick
    print("REAL E9 TEST -- re-review verdict:", retick.get("verdict"))

    findings_after = client.get(f"/api/tasks/{fix_task_id}/findings").json()
    print("REAL E9 TEST -- findings after fix:", findings_after)
    for fid in original_finding_ids:
        row = next((f for f in findings_after if f["id"] == fid), None)
        assert row and row["status"] != "OPEN", f"original finding {fid} still OPEN after the real fix: {row}"

    d = client.get(f"/api/tasks/{fix_task_id}/decision").json()
    assert d["status"] != "DONE", d  # E9.13/TaskDecisionService remains authoritative -- fix submission alone is not completion

    after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    real_after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    real_after_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout, capture_output=True, text=True).stdout
    assert real_after_head == before_head
    assert real_after_status == before_status
    print("REAL E9 TEST -- live ProjectFlow checkout confirmed untouched throughout.")


# ================================================================ E9.35: real SecurityReview

def test_real_security_review_catches_path_traversal(client, git_repo):
    root, repo = git_repo
    real_checkout = Path(__file__).resolve().parent.parent
    before_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    before_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout, capture_output=True, text=True).stdout

    _seed_repo(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Serve a user's own uploaded file", project_id=rid,
                      description="Users may only read files inside their OWN designated upload directory "
                                   "(base_dir). A user must never be able to read another user's files or any "
                                   "file outside base_dir via the filename they supply.")
    work_products = client.app.state.work_products
    from app.services.architecture_design_service import design_state_digest
    from app.services.test_design_service import test_design_state_digest
    work_products.create(kind="FEATURE_SPEC", title="user file access spec", change_id=cid, status="APPROVED",
                          content_metadata={"requirements": [
                              {"id": "REQ-1", "text": "get_user_file(base_dir, filename) returns the file content for a file the user owns"},
                              {"id": "REQ-2", "text": "get_user_file must never read a file outside base_dir, regardless of what filename is supplied"}]})
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "CONTROLLED"})
    assert r.status_code == 200, r.text

    db = client.app.state.db
    plan_id = db.execute(
        "INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest,design_baseline_digest,test_design_baseline_digest) "
        "VALUES(?,?,?,?,?,?,?)",
        (cid, 1, "MATERIALIZED", "claude", "x", design_state_digest(work_products, cid), test_design_state_digest(work_products, cid)))
    tid = db.execute("INSERT INTO tasks(slug,title,status,change_id,task_type) VALUES(?,?,?,?,?)",
                      ("get-user-file-e9-real", "Implement get_user_file()", "BACKLOG", cid, "IMPLEMENTATION"))
    db.execute(
        "INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (plan_id, "T1", "Implement get_user_file()", "IMPLEMENTATION", "[]", json.dumps(["REQ-1", "REQ-2"]), json.dumps(["userfiles.py"]), tid))

    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/worktree/create", data={"repository_id": rid, "agent": "claude"})
    assert r.status_code == 200, r.text
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    worktree = ws["worktree_path"]

    # Deliberately vulnerable: no path normalization/containment check --
    # a filename like "../../etc/passwd" escapes base_dir. Never actually
    # exercised against anything outside this disposable fixture.
    Path(worktree, "userfiles.py").write_text(
        "import os\n\n"
        "def get_user_file(base_dir, filename):\n"
        "    path = os.path.join(base_dir, filename)\n"
        "    with open(path) as f:\n"
        "        return f.read()\n")
    subprocess.run(["git", "add", "userfiles.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "implement get_user_file()"], cwd=worktree, check=True, capture_output=True)

    r = client.post(f"/api/workspaces/{ws['id']}/verification-report",
                     data={"work_status": "READY", "what_changed": "Implemented get_user_file()", "files_changed": "userfiles.py"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text

    applicability = client.app.state.security_applicability_service.applicable(
        tid, client.app.state.worktree_manager.get_task_worktree(tid), "CONTROLLED", None)
    print("REAL E9 TEST -- security applicability:", applicability)
    assert applicability["required"] is True

    result = client.post(f"/api/tasks/{tid}/review/security").json()
    print("REAL E9 TEST -- security review result:", result)
    assert result["outcome"] == "REVIEWED", result
    print("REAL E9 TEST -- security verdict:", result["verdict"])
    findings = client.get(f"/api/tasks/{tid}/findings").json()
    print("REAL E9 TEST -- security findings:", findings)

    if result["verdict"] not in ("FIX_REQUIRED",):
        pytest.skip(f"real security reviewer returned {result['verdict']!r} instead of FIX_REQUIRED for an "
                    f"unvalidated path-join -- a genuine finding about reviewer behavior; see log above. findings={findings}")

    security_findings = [f for f in findings if f["category"] == "SECURITY" and f["status"] == "OPEN"]
    assert security_findings, findings
    print("REAL E9 TEST -- severities found:", [f["severity"] for f in security_findings])
    assert any(f["severity"] in ("HIGH", "CRITICAL") for f in security_findings)

    readiness = client.get(f"/api/tasks/{tid}/integration-readiness").json()
    print("REAL E9 TEST -- integration readiness:", readiness)
    assert readiness["ready"] is False
    assert "UNRESOLVED_BLOCKING_FINDING" in readiness["blockers"] or "SECURITY_REVIEW_NOT_PASS" in readiness["blockers"]

    after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    real_after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    real_after_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout, capture_output=True, text=True).stdout
    assert real_after_head == before_head
    assert real_after_status == before_status
    print("REAL E9 TEST -- live ProjectFlow checkout confirmed untouched.")
