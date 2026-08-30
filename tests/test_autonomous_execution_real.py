"""E8.24: SAFE REAL TEST -- one real, non-fake autonomous Builder
execution against a disposable git-tracked fixture (the standard
pytest `git_repo`/`client` fixtures -- an isolated tmp_path repo and a
completely separate sqlite db, NEVER the live ProjectFlow checkout).

No fake launcher is installed anywhere in this file -- the Builder
session below is a genuine `claude --dangerously-skip-permissions` PTY
process (AGENT_LAUNCHERS["claude"], the exact launcher a manual "Start
Claude" click already uses in production), launched through
AutonomousExecutionService.tick() -> _launch() -> the app's own real
_start_builder_session(). No second launch mechanism exists here.

Verification never depends on interpreting the interactive CLI's own
PTY transcript -- nothing in this codebase auto-transitions a live
interactive session out of RUNNING (see
test_builder_completion_vs_session.py's own docstring: a human always
presses Confirm & Submit for a real agent). Instead this test polls the
ACTUAL fixture worktree on disk with a real `pytest` invocation against
the fixture's own failing test, which is unambiguous ground truth
regardless of what the agent's terminal shows."""
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

pytestmark = pytest.mark.skipif(shutil.which("claude") is None, reason="real `claude` CLI not installed on this host")


def _seed_buggy_fixture(repo):
    """E8.24's own example scenario, verbatim: a function that returns
    the wrong constant, and a real failing pytest test that expects the
    right one. Committed on top of the git_repo fixture's own base
    commit (never replacing PROJECT.yaml's required stages, only
    layering the fixture source + autonomous_execution policy onto it)."""
    (repo / "calc.py").write_text("def answer():\n    return 41\n")
    (repo / "test_calc.py").write_text("from calc import answer\n\n\ndef test_answer_is_42():\n    assert answer() == 42\n")
    # __pycache__/.pytest_cache are a side effect of running pytest
    # inside this repo (both this test's own pre/post checks and
    # anything the real Builder runs) -- ignored so they never register
    # as an uncommitted change and falsely trip DIRTY_WORKTREE_REQUIRES_
    # ATTENTION (E8.19), which is checking for genuinely unrelated work,
    # not test tooling byproducts.
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: demo}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\n"
        "ci: {required: [preflight, test]}\n"
        "engineering:\n  autonomous_execution:\n    enabled: true\n    max_concurrent_builders: 1\n    auto_start_ready_tasks: true\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed: answer() returns the wrong constant, test_calc.py expects 42"],
                    cwd=repo, check=True, capture_output=True)


CLAUDE_CONFIG = Path.home() / ".claude.json"
_TRUST_ENTRY = {"allowedTools": [], "mcpContextUris": [], "mcpServers": {}, "enabledMcpjsonServers": [],
                 "disabledMcpjsonServers": [], "hasTrustDialogAccepted": True,
                 "hasClaudeMdExternalIncludesApproved": False, "hasClaudeMdExternalIncludesWarningShown": False}


def _trust_claude_dir(path):
    """Claude Code shows a one-time 'is this a project you trust?'
    dialog for any directory it has never seen before, even under
    --dangerously-skip-permissions -- confirmed via this test's own
    earlier real run: the live PTY transcript showed exactly that
    prompt, then went silent forever (nothing in this test ever answers
    an interactive dialog). Production Builder Workspaces never hit
    this because they're created under .worktrees/ inside the already-
    -trusted live repo checkout; this test's disposable tmp_path root
    is a brand-new path Claude Code has never seen. Pre-registers trust
    the same way a real operator already has for their own project
    root, in ~/.claude.json's per-directory `projects` map (the exact
    field observed on this host's own already-trusted project entries).
    Returns the previous value (None if the key was new) so the caller
    can restore it afterward -- this only ever ADDS/restores one key,
    never touches any other entry in this shared, persistent file."""
    key = str(Path(path).resolve())
    data = json.loads(CLAUDE_CONFIG.read_text())
    previous = data.get("projects", {}).get(key)
    data.setdefault("projects", {})[key] = dict(_TRUST_ENTRY)
    CLAUDE_CONFIG.write_text(json.dumps(data))
    return key, previous


def _restore_claude_dir(key, previous):
    """Restores the one key this test explicitly set, PLUS sweeps any
    other /tmp/pytest-of-dell/... entry Claude Code itself may have
    additionally written during the real session (observed in practice:
    it also registers the worktree's main-repo path on its own, not
    just the exact key pre-seeded here) -- every such path is a
    disposable pytest tmp_path this test run owns, never anything a
    real user configured, so removing them all is safe cleanup, not
    data loss."""
    data = json.loads(CLAUDE_CONFIG.read_text())
    projects = data.get("projects", {})
    for other_key in [k for k in projects if k.startswith("/tmp/pytest-of-dell/")]:
        projects.pop(other_key, None)
    if previous is not None:
        projects[key] = previous
    CLAUDE_CONFIG.write_text(json.dumps(data))


def _pytest_passes(cwd) -> bool:
    import os
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_calc.py"],
                             cwd=str(cwd), capture_output=True, text=True, timeout=60, env=env)
    print("REAL E8 TEST -- pytest in", cwd, "->", result.returncode)
    print(result.stdout[-2000:])
    return result.returncode == 0


def test_real_autonomous_builder_fixture_end_to_end(client, git_repo):
    root, repo = git_repo
    real_checkout = Path(__file__).resolve().parent.parent
    before_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout,
                                  capture_output=True, text=True).stdout.strip()
    before_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout,
                                    capture_output=True, text=True).stdout

    _seed_buggy_fixture(repo)
    assert not _pytest_passes(repo)  # 1/2. confirm the bug is real before anything else

    rid = register(client, repo, "demo")
    cid = new_change(client, "Fix answer() to return the correct constant", project_id=rid,
                      description="calc.answer() currently returns 41; requirement REQ-1 says it must return 42, "
                                   "and test_calc.py::test_answer_is_42 already asserts this.")

    # -- Approved Spec/Design baseline (E5/E6 already have their OWN
    #    real-LLM tests for spec/design AUTHORING itself -- E8.24's
    #    subject is the Builder launch, so these upstream WorkProducts
    #    are seeded directly through the same service layer this
    #    file's own eligibility tests already use for every other
    #    required-state check). --
    from app.services.architecture_design_service import design_state_digest
    from app.services.test_design_service import test_design_state_digest
    work_products = client.app.state.work_products
    work_products.create(kind="FEATURE_SPEC", title="Fix answer()", change_id=cid, status="APPROVED",
                          content_metadata={"requirements": [{"id": "REQ-1", "text": "calc.answer() must return 42, not 41"}]})
    work_products.create(kind="TECHNICAL_DESIGN", title="Fix answer() design", change_id=cid, status="APPROVED",
                          content_metadata={"design_summary": "Change the literal return constant in calc.answer() from 41 to 42.",
                                             "components_to_change": ["calc.py"]})

    r = client.post(f"/api/changes/{cid}/workflow", data={"profile_key": "AGENTIC_STANDARD"})
    assert r.status_code == 200, r.text

    db = client.app.state.db
    # Pin the Plan's own design/test-design baseline digests to the
    # CURRENT design state above -- otherwise check_design_staleness()
    # (a real, correctly-firing E4.17/E6.17 check) sees stored=NULL vs a
    # real current digest and legitimately reports PLAN_DESIGN_STALE,
    # same discipline planner_service.create_plan() itself follows.
    plan_id = db.execute(
        "INSERT INTO plans(change_id,revision,status,planner_provider,input_context_digest,design_baseline_digest,test_design_baseline_digest) "
        "VALUES(?,?,?,?,?,?,?)",
        (cid, 1, "MATERIALIZED", "claude", "x",
         design_state_digest(work_products, cid), test_design_state_digest(work_products, cid)))
    tid = db.execute("INSERT INTO tasks(slug,title,status,change_id,task_type) VALUES(?,?,?,?,?)",
                      ("fix-answer-e8-real", "Fix answer() to return 42", "BACKLOG", cid, "IMPLEMENTATION"))
    db.execute(
        "INSERT INTO plan_items(plan_id,item_key,title,task_type,depends_on_keys,requirement_ids,scope_hints,materialized_task_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (plan_id, "T1", "Fix answer()", "IMPLEMENTATION", "[]", json.dumps(["REQ-1"]), json.dumps(["calc.py"]), tid))

    # _default_provider() picks the first of settings.agents that's
    # LAUNCHABLE, which defaults to "codex" ahead of "claude" -- but this
    # sandbox's `codex` CLI is unauthenticated (its real PTY transcript,
    # captured by an earlier run of this same test, shows codex's own
    # onboarding/auth screen and then goes silent forever). `claude` is
    # the one CLI confirmed working in this environment -- this whole
    # test suite's own session runs as an authenticated `claude` process,
    # and E5-E7's own real-LLM tests already invoke `claude` successfully
    # -- so pin provider selection to it for this real end-to-end run.
    svc = client.app.state.autonomous_execution_service
    svc.settings = dataclasses.replace(svc.settings, agents=("claude",))

    # Default prompt_ready_timeout/prompt_quiet_window (8.0s/0.4s) are
    # tuned against every existing test's FAKE launcher (a trivial `bash
    # -c 'echo READY; cat'`). A real, freshly-started, nested Claude Code
    # TUI is heavier -- confirmed via this test's own earlier run: prompt_
    # status reached DELIVERED (a real write() into the PTY succeeded)
    # but the live transcript never echoed the prompt text at all, and
    # the session stayed idle at RUNNING for the rest of the test --
    # consistent with the write racing the CLI's own still-settling
    # startup redraw (the "/rc connecting..." resource-check step seen
    # in that transcript) and being silently swallowed. A longer,
    # test-only quiet window (still the same tunable knobs every other
    # test in this repo already overrides for its own fake launcher)
    # gives the real CLI room to fully settle before delivery.
    client.app.state.agent_sessions.prompt_ready_timeout = 25.0
    client.app.state.agent_sessions.prompt_quiet_window = 2.0

    # Pre-register trust (see _trust_claude_dir's own docstring) for the
    # EXACT worktree path git.create_agent() is about to create --
    # computed with the app's own slugify(), the same deterministic
    # naming add_task_workspace() uses, so this test never guesses.
    from app.services.git_workspace import slugify
    worktree_path_precomputed = root / ".worktrees" / f"{slugify('demo')}-{slugify('claude')}-{slugify('fix-answer-e8-real-demo')}"
    trust_key, trust_previous = _trust_claude_dir(worktree_path_precomputed)
    print("REAL E8 TEST -- pre-trusted worktree path for Claude Code:", trust_key)

    readiness = client.get(f"/api/tasks/{tid}/execution-readiness").json()
    print("REAL E8 TEST -- readiness before tick:", readiness)
    assert readiness["readiness"] == "AUTO_READY", readiness  # 5. evaluate auto readiness

    r = client.post(f"/api/changes/{cid}/autonomous-execution/tick")  # 6. autonomous tick
    body = r.json()
    print("REAL E8 TEST -- tick result:", body)
    assert body["launched"], body
    result = body["results"][0]
    assert result["outcome"] == "LAUNCHED", result  # 7. real Claude Builder launch
    sid, wid = result["session_id"], result["workspace_id"]
    ws = db.one("SELECT * FROM agent_workspaces WHERE id=?", (wid,))
    worktree = ws["worktree_path"]
    print("REAL E8 TEST -- real claude session id:", sid, "provider:", ws["agent"], "worktree:", worktree)
    assert str(Path(worktree).resolve()) == trust_key, "worktree path prediction was wrong -- trust was registered for the wrong path"

    try:
        deadline = time.time() + 420
        fixed = False
        last_tail_len = 0
        next_tail_print = time.time()
        while time.time() < deadline:
            if _pytest_passes(worktree):  # 8/9. Builder edits fixture; real test passes
                fixed = True
                break
            if time.time() >= next_tail_print:
                tail = client.app.state.agent_sessions.live_tail(sid) or ""
                print(f"REAL E8 TEST -- live transcript tail ({len(tail)} bytes so far):")
                print(tail[last_tail_len:][-4000:])
                last_tail_len = len(tail)
                next_tail_print = time.time() + 30
            time.sleep(5)
        print("REAL E8 TEST -- final calc.py:", (Path(worktree) / "calc.py").read_text())
        assert fixed, "real Claude Builder did not make test_calc.py pass within the timeout"

        # 10/11. WorkProduct/evidence capture + Task post-builder state --
        # the real Builder's own prompt instructs it to commit and Submit
        # for Review, but this test does not wait on that indefinitely
        # (the fix + real passing test above is already the ground-truth
        # proof E8.24 asks for). If the agent got there in time, exercise
        # the REAL submission path too, end-to-end, for real evidence.
        commit_deadline = time.time() + 60
        committed = False
        while time.time() < commit_deadline:
            if not client.app.state.git.status(worktree).strip():
                committed = True
                break
            time.sleep(5)
        if committed:
            r = client.post(f"/api/workspaces/{wid}/verification-report",
                             data={"work_status": "READY", "what_changed": "Fixed calc.answer() to return 42",
                                   "files_changed": "calc.py", "automated_tests": "pytest -q test_calc.py -> 1 passed"})
            print("REAL E8 TEST -- verification-report submit status:", r.status_code)
            wp = db.one("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))
            print("REAL E8 TEST -- CODE_CHANGE WorkProduct:", dict(wp) if wp else None)
            d = client.get(f"/api/tasks/{tid}/decision").json()
            print("REAL E8 TEST -- Task decision after Builder submission:", d["status"], d["stage"])
            # E8.13: agent finishing its turn must never itself equal
            # TASK COMPLETE -- TaskDecisionService's own review/
            # verification boundary is still authoritative.
            assert d["status"] != "DONE", "a Builder submitting evidence must not, by itself, complete the Task"
        else:
            print("REAL E8 TEST -- agent did not reach a clean committed state within the grace period; "
                  "the file-level fix + passing test above already satisfies E8.24's core requirement, "
                  "and E8.12/E8.18's own mechanisms are separately verified by tests/test_autonomous_execution.py's "
                  "deterministic fake-launcher tests.")
    finally:
        # A real interactive Claude Code session never exits on its own
        # after finishing a turn -- cleanup is always this test's own
        # responsibility, same discipline test_builder_completion_vs_
        # session.py's _stop_lingering_sessions autouse fixture uses.
        try:
            client.app.state.agent_sessions.stop(sid)
        except Exception:
            pass
        try:
            _restore_claude_dir(trust_key, trust_previous)
        except Exception:
            pass

    # 12. confirm NO unrelated repository (the live ProjectFlow checkout
    #     this test itself runs from) was modified.
    after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    after_status = subprocess.run(["git", "status", "--porcelain"], cwd=real_checkout, capture_output=True, text=True).stdout
    assert after_head == before_head, "the live ProjectFlow checkout's HEAD must never move during this test"
    assert after_status == before_status, "the live ProjectFlow checkout's working tree must never change during this test"
    print("REAL E8 TEST -- live ProjectFlow checkout confirmed untouched (HEAD and working tree both unchanged).")
