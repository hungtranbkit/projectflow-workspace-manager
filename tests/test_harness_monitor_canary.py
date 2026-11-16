"""Fail-closed technical monitor canary tests without starting live units."""
from pathlib import Path

import pytest

from app.services.agent_harness_mvp import HarnessError, HarnessStore
from scripts.harness_monitor_bridge import bootstrap, load_registry
from scripts.harness_monitor_canary import CHILD_GOALS, run_one


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "harness.db"
    HarnessStore(path)
    bootstrap(load_registry(), path)
    return path


class FakeManager:
    calls = []

    def __init__(self, db_path, state_root):
        self.db_path = db_path

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        task = HarnessStore(self.db_path).create(
            key=f"canary-{len(self.calls)}", main_goal=kwargs["main_goal"],
            child_goal=kwargs["child_goal"], dod="one verified echo",
            agent="shell", node="dell-linux",
            created_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc))
        return {"state": task.state, "task_id": task.id,
                "unit": "harness-child-canary.service",
                "deadline": task.expires_at.isoformat()}


def test_canary_observe_only_by_default(db, tmp_path):
    selected = next(iter(CHILD_GOALS))
    result = run_one(selected, db, tmp_path)
    assert result["state"] == "NOT_LAUNCHED"
    assert not HarnessStore(db).list_tasks()


def test_canary_keeps_main_goal_and_does_not_duplicate(db, tmp_path):
    FakeManager.calls = []
    selected = next(iter(CHILD_GOALS))
    result = run_one(selected, db, tmp_path, launch=True, manager_cls=FakeManager)
    assert result["project_DoD"] == "NOT_TESTED_BY_CANARY"
    assert len(FakeManager.calls) == 1
    binding_goal = load_registry()[selected]["main_goal"]
    assert FakeManager.calls[0]["main_goal"] == binding_goal
    repeated = run_one(selected, db, tmp_path, launch=True, manager_cls=FakeManager)
    assert repeated["repeat_launch_refused"] is True
    assert len(FakeManager.calls) == 1
    assert len(HarnessStore(db).list_tasks()) == 1


def test_paused_and_maintenance_cannot_launch(db, tmp_path):
    registry = load_registry()
    blocked = [ident for ident, row in registry.items()
               if not row["configured_enabled"] or row["role"] == "maintenance"]
    assert blocked
    for ident in blocked:
        with pytest.raises(HarnessError, match="only reviewed"):
            run_one(ident, db, tmp_path, launch=True, manager_cls=FakeManager)
    assert not HarnessStore(db).list_tasks()


def test_canary_wip_gate_protects_real_tasks(db, tmp_path):
    from datetime import datetime, timezone
    ident = next(iter(CHILD_GOALS))
    goal = load_registry()[ident]["main_goal"]
    store = HarnessStore(db)
    for k in range(2):
        store.create(key=f"real-{k}", main_goal=goal,
                     child_goal=f"real work {k}", dod="business DoD",
                     agent="codex", node="dell-linux",
                     created_at=datetime.now(timezone.utc))
    with pytest.raises(HarnessError, match="WIP limit"):
        run_one(ident, db, tmp_path, launch=True, manager_cls=FakeManager)
