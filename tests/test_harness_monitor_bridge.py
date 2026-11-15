"""Safe no-network tests for ChatGPT automation ↔ Harness binding."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.services.agent_harness_mvp import HarnessError, HarnessStore
from scripts import harness_monitor_bridge as bridge


@pytest.fixture
def source():
    return bridge.load_registry()


@pytest.fixture
def wired(tmp_path, source):
    db = tmp_path / "state.db"
    HarnessStore(db)  # existing live Harness schema must exist before bootstrap
    receipt = bridge.bootstrap(source, db)
    assert receipt["total"] == 25
    return db


def active_parent(source):
    return next((id for id, x in source.items()
                 if x["configured_enabled"] and x["role"] == "parent"))


def paused(source):
    return next((id for id, x in source.items() if not x["configured_enabled"]))


def backend(task_id="dt_proof_001", session="harness-demo", agent="codex",
            state="RUNNING", updated_at=None):
    return {
        "status": "OK",
        "result": {"status": "OK", "task": {
            "task_id": task_id, "target": session,
            "agent": agent, "mode": "agent", "state": state,
            "running_confirmed": True, "turn_state": state,
            "updated_at": (updated_at or datetime.now(timezone.utc).timestamp()),
            "watcher": {"alive": True},
        }},
    }


def test_inventories_all_existing_automations_without_enabling(source, wired):
    assert len(source) == 25
    assert sum(x["configured_enabled"] for x in source.values()) == 3
    assert sum(not x["configured_enabled"] for x in source.values()) == 22
    status = bridge.status(source, wired, None)
    assert status["count"] == 25
    assert status["active"] == 3
    assert status["paused"] == 22
    assert all(x["mode"] == "paused" and x["main_goal"] is None
               for x in status["monitors"] if not x["enabled_at_import"])


def test_bootstrap_reuse_and_immutable_main_goal(source, wired):
    assert bridge.bootstrap(source, wired) == {
        "registered": 0, "already": 25, "total": 25}
    chosen = active_parent(source)
    mod = {key: value.copy() for key, value in source.items()}
    mod[chosen]["main_goal"] = "silently changed project goal"
    with pytest.raises(HarnessError, match="immutable"):
        bridge.bootstrap(mod, wired)
    with pytest.raises(HarnessError, match="GOAL_DRIFT"):
        bridge.status(mod, wired, chosen)


def test_external_receipt_is_dispatched_only_and_idempotent(source, wired):
    ident = active_parent(source)
    opts = dict(
        registry=source, db_path=wired, ident=ident,
        child_goal="one small source audit", dod="one independent source test",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=1800,
        backend=backend(),
    )
    one = bridge.register(**opts)
    assert one["state"] == "DISPATCHED"
    assert one["RUNNING_VERIFIED"] is False
    duplicate = bridge.register(**opts)
    assert duplicate["task_id"] == one["task_id"]
    assert duplicate["idempotent"] is True
    status = bridge.status(source, wired, ident)
    child = status["monitors"][0]["child_tasks"][0]
    assert child["state"] == "DISPATCHED"
    assert child["external_task"] is True


def test_wrong_target_and_paused_registration_are_denied(source, wired):
    ident = active_parent(source)
    kw = dict(
        registry=source, db_path=wired, ident=ident, child_goal="read-only test",
        dod="test output verified", agent="codex", node="dell-linux",
        session_name="harness-demo", task_id="dt_proof_001",
        ttl_seconds=900, backend=backend(session="another-session"),
    )
    with pytest.raises(HarnessError, match="target/agent"):
        bridge.register(**kw)
    kw["ident"] = paused(source)
    kw["backend"] = backend()
    with pytest.raises(HarnessError, match="paused/maintenance"):
        bridge.register(**kw)


def test_two_active_child_limit_enforced(source, wired):
    ident = active_parent(source)
    for number in (1, 2):
        bridge.register(
            registry=source, db_path=wired, ident=ident,
            child_goal=f"independent step {number}", dod="one test per step",
            agent="codex", node="dell-linux", session_name="harness-demo",
            task_id=f"dt_proof_{number:03d}", ttl_seconds=1800,
            backend=backend(task_id=f"dt_proof_{number:03d}"),
        )
    with pytest.raises(HarnessError, match="WIP limit"):
        bridge.register(
            registry=source, db_path=wired, ident=ident,
            child_goal="unexpected third task", dod="one test",
            agent="codex", node="dell-linux", session_name="harness-demo",
            task_id="dt_proof_003", ttl_seconds=1800,
            backend=backend(task_id="dt_proof_003"),
        )


def test_remote_DONE_remains_unaccepted_without_independent_DoD(source, wired, monkeypatch):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="external completed agent", dod="runtime E2E acceptance",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(),
    )

    async def remote(_):
        return backend(state="DONE")

    monkeypatch.setattr(bridge, "verify_external_task", remote)
    result = asyncio.run(bridge.reconcile_external(source, wired))
    assert result["checked"] == 1
    assert result["results"][0]["task_state"] == "NOT_RUNNING"
    assert HarnessStore(wired).get(task["task_id"]).state != "DONE_VERIFIED"


def test_remote_running_requires_fresh_backend_progress(source, wired, monkeypatch):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="external still running", dod="one independent test",
        agent="codex", node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600,
        backend=backend(),
    )

    async def remote(_):
        return backend()

    monkeypatch.setattr(bridge, "verify_external_task", remote)
    asyncio.run(bridge.reconcile_external(source, wired))
    assert HarnessStore(wired).get(task["task_id"]).state == "RUNNING_VERIFIED"


def test_backend_failure_does_not_lie_about_progress(source, wired, monkeypatch):
    ident = active_parent(source)
    task = bridge.register(
        registry=source, db_path=wired, ident=ident,
        child_goal="unavailable backend", dod="test", agent="codex",
        node="dell-linux", session_name="harness-demo",
        task_id="dt_proof_001", ttl_seconds=3600, backend=backend(),
    )

    async def offline(_):
        raise HarnessError("MCP offline")

    monkeypatch.setattr(bridge, "verify_external_task", offline)
    result = asyncio.run(bridge.reconcile_external(source, wired))
    assert result["results"][0]["backend_state"] == "UNKNOWN"
    assert HarnessStore(wired).get(task["task_id"]).state == "DISPATCHED"


def test_mcp_decoder_rejects_unstructured_or_error():
    from types import SimpleNamespace
    with pytest.raises(HarnessError):
        bridge.decode_mcp(SimpleNamespace(is_error=True))
    with pytest.raises(HarnessError):
        bridge.decode_mcp(SimpleNamespace(is_error=False, structured_content=None,
                                          content=[SimpleNamespace(text="not json")]))
