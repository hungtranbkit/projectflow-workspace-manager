"""Issue #118: monitor decision -> durable Terminal MCP task, execution-first.

No network: `turn` (terminal_turn start) and `verify` (task readback) are
injected fakes shaped like terminal-mcp PR #120 responses.
"""
import asyncio

import pytest

from app.services.agent_harness_mvp import HarnessError, HarnessStore
from scripts import harness_monitor_bridge as bridge
from test_harness_monitor_bridge import active_parent, backend, paused, source, wired  # noqa: F401

DT = "dt_0123456789abcdef"


class FakeMCP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def turn(self, arguments):
        self.calls.append(arguments)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def verify(self, task_id):
        return backend(task_id=task_id, state="EXECUTION_ACCEPTED")


def receipt(task_id=DT, status="TASK_ACCEPTED"):
    return {"status": status, "action": "start", "mode": "supervised", "task_id": task_id,
            "task_state": "EXECUTION_ACCEPTED", "session": "harness-demo"}


def dispatch(source, db, mcp, child="atomic child", **kw):  # noqa: F811
    return asyncio.run(bridge.dispatch_child(
        source, db, active_parent(source), child_goal=child, dod="one independent test",
        agent="codex", node="dell-linux", session_name="harness-demo", ttl_seconds=3600,
        turn=mcp.turn, verify=mcp.verify, **kw))


def attempts(db):
    with bridge.connect(db) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM harness_dispatch_attempts ORDER BY id")]


def test_durable_receipt_registers_harness_task_dispatched_not_running(source, wired):  # noqa: F811
    mcp = FakeMCP(receipt())
    out = dispatch(source, wired, mcp)
    assert out["outcome"] == "DISPATCHED" and out["external_task_id"] == DT
    task = HarnessStore(wired).get(out["harness_task_id"])
    assert task.state == "DISPATCHED" and task.external_task_id == DT
    assert out["RUNNING_VERIFIED"] is False and out["counts_as_work"] is False
    (call,) = mcp.calls
    assert call["action"] == "start" and call["target"] == "harness-demo"
    assert call["request_key"] == out["request_key"] and call["request_key"].startswith("pf-")


@pytest.mark.parametrize("response", [
    {"status": "SUBMIT_CONFIRMED", "action": "send", "result": {}},           # 56ccb14 non-Codex
    {"status": "TASK_STARTED", "mode": "direct", "task_id": None},             # 56ccb14 Codex
    {"status": "TASK_ACCEPTED", "mode": "supervised", "task_id": "not-a-dt"},
])
def test_null_or_untracked_task_id_is_dispatch_failed_never_work(source, wired, response):  # noqa: F811
    mcp = FakeMCP(response)
    out = dispatch(source, wired, mcp)
    assert out["outcome"] == "DISPATCH_FAILED" and out["error"] == "NULL_TASK_ID"
    assert out["counts_as_work"] is False
    assert out["escalation"].startswith("UPGRADE_CONTROLLER_DURABLE_START")
    assert HarnessStore(wired).list_tasks() == []
    # Not retryable: the next monitor cycle escalates without another send.
    again = dispatch(source, wired, mcp)
    assert again["outcome"] == "ESCALATED" and len(mcp.calls) == 1


def test_idle_refusal_retries_same_key_bounded_then_escalates(source, wired):  # noqa: F811
    busy = {"status": "FAILED", "error": "DIRECT_TARGET_NOT_IDLE", "task_state": "RUNNING"}
    mcp = FakeMCP(busy)
    outcomes = [dispatch(source, wired, mcp)["outcome"] for _ in range(5)]
    assert outcomes == ["DISPATCH_FAILED"] * 3 + ["ESCALATED"] * 2
    assert len(mcp.calls) == bridge.MAX_DISPATCH_ATTEMPTS
    assert len({c["request_key"] for c in mcp.calls}) == 1          # never a new task
    assert "RETRY_BUDGET_EXHAUSTED" in attempts(wired)[-1]["escalation"]
    assert HarnessStore(wired).list_tasks() == []


def test_retry_after_idle_succeeds_with_the_same_key(source, wired):  # noqa: F811
    busy = {"status": "FAILED", "error": "DIRECT_TARGET_NOT_IDLE"}
    mcp = FakeMCP(busy, receipt())
    assert dispatch(source, wired, mcp)["outcome"] == "DISPATCH_FAILED"
    out = dispatch(source, wired, mcp)
    assert out["outcome"] == "DISPATCHED" and out["attempt"] == 2
    assert mcp.calls[0]["request_key"] == mcp.calls[1]["request_key"]


def test_restart_replay_never_sends_again(source, wired):  # noqa: F811
    first = dispatch(source, wired, FakeMCP(receipt()))
    fresh = FakeMCP(AssertionError("must not call Terminal MCP on replay"))
    again = dispatch(source, wired, fresh)   # new process: only the DB survives
    assert again["idempotent"] is True and again["external_task_id"] == DT
    assert again["harness_task_id"] == first["harness_task_id"] and fresh.calls == []


def test_transport_failure_is_retryable_dispatch_failed(source, wired):  # noqa: F811
    mcp = FakeMCP(ConnectionError("controller down"))
    out = dispatch(source, wired, mcp)
    assert out["outcome"] == "DISPATCH_FAILED" and out["error"] == "CONTROLLER_UNREACHABLE"
    assert HarnessStore(wired).list_tasks() == []


def test_readback_mismatch_is_failed_and_not_registered(source, wired):  # noqa: F811
    mcp = FakeMCP(receipt())

    async def wrong(task_id):
        return backend(task_id=task_id, session="someone-else")

    mcp.verify = wrong
    out = dispatch(source, wired, mcp)
    assert out["outcome"] == "DISPATCH_FAILED" and out["error"] == "READBACK_MISMATCH"
    assert HarnessStore(wired).list_tasks() == []


def test_paused_monitor_and_wip_limit_refuse_before_sending(source, wired):  # noqa: F811
    mcp = FakeMCP(receipt())
    with pytest.raises(HarnessError):
        asyncio.run(bridge.dispatch_child(
            source, wired, paused(source), child_goal="x", dod="y", agent="codex",
            node="dell-linux", session_name="harness-demo", ttl_seconds=60,
            turn=mcp.turn, verify=mcp.verify))
    for n, task_id in enumerate(("dt_aaaaaaaaaaaaaaaa", "dt_bbbbbbbbbbbbbbbb")):
        assert dispatch(source, wired, FakeMCP(receipt(task_id)), child=f"child {n}")["outcome"] == "DISPATCHED"
    with pytest.raises(HarnessError, match="WIP"):
        dispatch(source, wired, mcp, child="third child")
    assert mcp.calls == []


@pytest.mark.parametrize("state,expected", [
    ("EXECUTION_ACCEPTED", "UNKNOWN"),
    ("DISPATCH_UNCERTAIN", "BLOCKED"),
    ("STUCK_COMPOSER", "BLOCKED"),
])
def test_reconcile_understands_supervisor_states(source, wired, monkeypatch, state, expected):  # noqa: F811
    out = dispatch(source, wired, FakeMCP(receipt()))

    async def remote(_):
        return backend(task_id=DT, state=state)

    monkeypatch.setattr(bridge, "verify_external_task", remote)
    result = asyncio.run(bridge.reconcile_external(source, wired))
    assert "error" not in result["results"][0], result
    assert HarnessStore(wired).get(out["harness_task_id"]).state == expected
