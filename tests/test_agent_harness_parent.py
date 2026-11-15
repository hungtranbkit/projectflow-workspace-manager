"""No-network parent handoff tests: no duplicated child, no fake main PASS."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from app.services.agent_harness_mvp import HarnessStore
from scripts.harness_parent import validate, tick_one


T = datetime.now(timezone.utc)


def plan(repo):
    return {
        "plan_id": "parent-qa-test",
        "main_goal": "One immutable main test goal",
        "enabled": True,
        "steps": [
            {"goal": "one output", "expected": "MARKER_1", "repo": str(repo),
             "executor": "echo-probe", "ttl_seconds": 3600},
            {"goal": "second output", "expected": "MARKER_2", "repo": str(repo),
             "executor": "echo-probe", "ttl_seconds": 3600},
        ],
        "final_acceptance": {"repo": str(repo), "test": "tests/test_accept.py"},
    }


def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests/test_accept.py").write_text("def test_accept(): assert False\n")
    return repo


def test_parent_refuses_nested_or_compound_duplicate_goal(tmp_path):
    p = plan(fixture_repo(tmp_path))
    validate(p)
    p["steps"][1]["goal"] = "one output"
    with pytest.raises(ValueError, match="duplicate"):
        validate(p)
    p = plan(tmp_path / "repo")
    p["steps"][0]["ttl_seconds"] = 86401
    with pytest.raises(ValueError, match="24h"):
        validate(p)


def test_parent_does_not_dispatch_another_child_while_one_is_outstanding(tmp_path):
    repo = fixture_repo(tmp_path)
    p = plan(repo)
    store = HarnessStore(tmp_path / "harness.db")
    child = store.create(
        key="first", main_goal=p["main_goal"],
        child_goal=p["steps"][0]["goal"], dod="one proof",
        agent="codex", node="dell-linux", created_at=T, ttl=timedelta(hours=1))
    store.dispatch(child.id, external_task_id="receipt-only", at=T)
    def forbidden(**kwargs):
        raise AssertionError("parent attempted duplicate launch")
    fake = SimpleNamespace(store=store, launch=forbidden)
    response = tick_one(p, fake, tmp_path)
    assert response["state"] == "CHILD_OUTSTANDING"
    assert response["child_state"] == "DISPATCHED"
    assert len(store.list_tasks(main_goal=p["main_goal"])) == 1


def test_parent_never_claims_done_without_independent_main_test(tmp_path):
    repo = fixture_repo(tmp_path)
    p = plan(repo)
    store = HarnessStore(tmp_path / "harness.db")
    for index, step in enumerate(p["steps"]):
        t = store.create(
            key=f"done-{index}", main_goal=p["main_goal"], child_goal=step["goal"],
            dod="one proof", agent="codex", node="dell-linux",
            created_at=T, ttl=timedelta(hours=1))
        store.dispatch(t.id, external_task_id=f"task-{index}", at=T)
        store.observe(t.id, backend_state="FINISHED", process_alive=False, at=T)
        store.accept(t.id, at=T, artifact_ref="artifact", test_evidence="exit0",
                     runtime_readback="readback", verifier=lambda a,b,c: True)
        store.cleanup(t.id, at=T, executor_stopped=True, no_active_refs=True,
                      dirty_worktree=False, evidence_preserved=True, deletion_verified=True)
    fake = SimpleNamespace(store=store, launch=lambda **kwargs: None)
    response = tick_one(p, fake, tmp_path)
    assert response["state"] == "BLOCKED_ACCEPTANCE"
    assert response["final_test_exit"] != 0
    assert not (tmp_path / "parents/parent-qa-test.status.json").exists()
