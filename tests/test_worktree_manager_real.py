"""E8.5.28/E8.5.29/E8.5.30: SAFE REAL TEST for Worktree Isolation.
Reuses the exact real-CLI harness proven in
tests/test_autonomous_execution_real.py (trust pre-registration for a
never-seen directory, provider pinned to claude, widened prompt-
delivery timing for a real nested Claude Code startup) -- same
disposable fixture discipline, never the live ProjectFlow checkout.
This file adds the WORKTREE-specific assertions E8.5 itself is about:
branch/base identity, Task commit, CODE_CHANGE worktree/branch/base/
head trace, REVIEW_PENDING retention, integration-check, and safe
explicit cleanup."""
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
from tests.test_autonomous_execution_real import (
    _seed_buggy_fixture, _pytest_passes, _trust_claude_dir, _restore_claude_dir,
)

pytestmark = pytest.mark.skipif(shutil.which("claude") is None, reason="real `claude` CLI not installed on this host")


def test_real_worktree_isolation_fixture_end_to_end(client, git_repo):
    root, repo = git_repo
    real_checkout = Path(__file__).resolve().parent.parent
    before_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    before_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout, capture_output=True, text=True).stdout

    _seed_buggy_fixture(repo)
    assert not _pytest_passes(repo)  # 1. canonical checkout: bug is real

    rid = register(client, repo, "demo")
    cid = new_change(client, "Fix answer() via managed worktree", project_id=rid,
                      description="calc.answer() must return 42 per REQ-1; test_calc.py already asserts this.")
    work_products = client.app.state.work_products
    from app.services.architecture_design_service import design_state_digest
    from app.services.test_design_service import test_design_state_digest
    work_products.create(kind="FEATURE_SPEC", title="Fix answer()", change_id=cid, status="APPROVED",
                          content_metadata={"requirements": [{"id": "REQ-1", "text": "calc.answer() must return 42, not 41"}]})
    work_products.create(kind="TECHNICAL_DESIGN", title="Fix answer() design", change_id=cid, status="APPROVED",
                          content_metadata={"design_summary": "Change the literal return constant in calc.answer() from 41 to 42."})
    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    assert r.status_code == 200, r.text

    db = client.app.state.db
    plan_id = db.execute(
        "INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest,design_baseline_digest,test_design_baseline_digest) "
        "VALUES(?,?,?,?,?,?,?)",
        (cid, 1, "MATERIALIZED", "claude", "x", design_state_digest(work_products, cid), test_design_state_digest(work_products, cid)))
    tid = db.execute("INSERT INTO tasks(slug,title,status,change_id,task_type) VALUES(?,?,?,?,?)",
                      ("fix-answer-e85-real", "Fix answer() via worktree", "BACKLOG", cid, "IMPLEMENTATION"))
    db.execute(
        "INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (plan_id, "T1", "Fix answer()", "IMPLEMENTATION", "[]", json.dumps(["REQ-1"]), json.dumps(["calc.py"]), tid))

    svc = client.app.state.autonomous_execution_service
    svc.settings = dataclasses.replace(svc.settings, agents=("claude",))
    client.app.state.agent_sessions.prompt_ready_timeout = 25.0
    client.app.state.agent_sessions.prompt_quiet_window = 2.0

    readiness = client.get(f"/api/tasks/{tid}/execution-readiness").json()
    print("REAL E8.5 TEST -- readiness:", readiness)
    assert readiness["readiness"] == "AUTO_READY", readiness

    # -- pre-register trust for the exact worktree path (same technique
    #    proven in test_autonomous_execution_real.py) --
    from app.services.git_workspace import slugify
    worktree_path_precomputed = root / ".worktrees" / f"{slugify('demo')}-{slugify('claude')}-{slugify('fix-answer-e85-real-demo')}"
    trust_key, trust_previous = _trust_claude_dir(worktree_path_precomputed)

    r = client.post(f"/api/changes/{cid}/autonomous-execution/tick")
    body = r.json()
    print("REAL E8.5 TEST -- tick result:", body)
    assert body["launched"], body
    result = body["results"][0]
    assert result["outcome"] == "LAUNCHED", result
    sid, wid = result["session_id"], result["workspace_id"]

    # 2/3/4. worktree identity: NOT the canonical checkout, real branch/base
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    print("REAL E8.5 TEST -- managed worktree:", ws)
    assert Path(ws["worktree_path"]).resolve() != repo.resolve()
    assert ws["branch"].startswith("agent/claude/")
    assert ws["base_commit"]
    assert str(Path(ws["worktree_path"]).resolve()) == trust_key, "worktree path prediction was wrong -- trust registered for the wrong path"
    worktree = ws["worktree_path"]

    try:
        deadline = time.time() + 420
        fixed = False
        while time.time() < deadline:
            if _pytest_passes(worktree):  # 7/8. Builder edits WORKTREE; real test passes there
                fixed = True
                break
            time.sleep(5)
        print("REAL E8.5 TEST -- final calc.py:", (Path(worktree) / "calc.py").read_text())
        assert fixed, "real Claude Builder did not make test_calc.py pass in the managed worktree within the timeout"

        # 9. Task commit exists (allow a short grace period, same as E8.24's own real test)
        commit_deadline = time.time() + 60
        committed = False
        while time.time() < commit_deadline:
            if not client.app.state.git.status(worktree).strip():
                committed = True
                break
            time.sleep(5)
        assert committed, "Builder fixed the file but never committed within the grace period"

        r = client.post(f"/api/workspaces/{wid}/verification-report",
                         data={"work_status": "READY", "what_changed": "Fixed calc.answer() to return 42",
                               "files_changed": "calc.py"}, follow_redirects=False)
        assert r.status_code == 303, r.text

        # 10. CODE_CHANGE WorkProduct references worktree/branch/base/head
        wp = db.one("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))
        meta = json.loads(wp["content_metadata"])
        print("REAL E8.5 TEST -- CODE_CHANGE metadata:", meta)
        assert meta["worktree_path"] == worktree
        assert meta["branch_name"] == ws["branch"]
        assert meta["base_commit"] == ws["base_commit"]
        assert meta["head_commit"]
        assert len(meta["commits"]) >= 1
        assert meta["scope_check"]["violation"] is False
        assert meta["canonical_repo_check"]["checked"] is True
        assert meta["canonical_repo_check"]["modified"] is False  # 5. canonical checkout genuinely never touched

        # 14. Task remains at the REVIEW boundary, never auto-DONE
        d = client.get(f"/api/tasks/{tid}/decision").json()
        assert d["status"] != "DONE", d

        # 15. worktree retained as REVIEW_PENDING
        ws_after = client.get(f"/api/tasks/{tid}/worktree").json()
        assert ws_after["lifecycle_status"] == "REVIEW_PENDING", ws_after
        assert Path(ws_after["worktree_path"]).is_dir()

        # 16. integration-check returns clean (fixture's own base never moved)
        integ = client.get(f"/api/tasks/{tid}/integration-check").json()
        print("REAL E8.5 TEST -- integration-check:", integ)
        assert integ["result"] == "CLEAN", integ
    finally:
        try:
            client.app.state.agent_sessions.stop(sid)
        except Exception:
            pass
        try:
            _restore_claude_dir(trust_key, trust_previous)
        except Exception:
            pass

    # 11/12/13. canonical checkout byte-for-byte unchanged throughout
    assert repo.is_dir()
    # calc.py in the CANONICAL checkout stays exactly at its seeded
    # (buggy) content -- the fix only ever landed in the isolated worktree.
    assert (repo / "calc.py").read_text() == "def answer():\n    return 41\n"

    real_checkout_after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    real_checkout_after_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout, capture_output=True, text=True).stdout
    assert real_checkout_after_head == before_head
    assert real_checkout_after_status == before_status
    print("REAL E8.5 TEST -- live ProjectFlow checkout confirmed untouched.")

    # 17. explicit fixture cleanup removes only the managed worktree
    r = client.post(f"/api/tasks/{tid}/worktree/abandon")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/tasks/{tid}/worktree/cleanup")
    assert r.status_code == 200, r.text
    assert not Path(worktree).is_dir()
    assert repo.is_dir()
    print("REAL E8.5 TEST -- managed worktree explicitly cleaned up; canonical repo untouched.")
