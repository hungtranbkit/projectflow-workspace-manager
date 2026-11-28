"""Bounded no-network tests of Terminal MCP adapter verification rules."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from datetime import datetime, timezone
from app.services.agent_harness_terminal import (
    TerminalMCPAdapter, TerminalAdapterError, decode_tool_response,
)


class FakeMCP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        assert self.responses, "unexpected backend call"
        data = self.responses.pop(0)
        return SimpleNamespace(is_error=False, structured_content=None,
                               content=[SimpleNamespace(text=json.dumps(data))])


def test_dispatch_receipt_never_implies_running():
    asyncio.run(_test_dispatch_receipt_never_implies_running())

async def _test_dispatch_receipt_never_implies_running():
    backend = FakeMCP([{"status": "SUBMIT_CONFIRMED", "result": {"submission_id": "abc"}}])
    adapter = TerminalMCPAdapter(backend)
    receipt = await adapter.dispatch("harness-probe-1234", "one task", request_key="probe-1234")
    assert receipt.task_id == "abc"
    assert receipt.submitted is True
    assert receipt.running_verified is False
    assert backend.calls[0][1]["action"] == "start"


def test_create_rejects_unsafe_name_and_cwd():
    asyncio.run(_test_create_rejects_unsafe_name_and_cwd())

async def _test_create_rejects_unsafe_name_and_cwd():
    adapter = TerminalMCPAdapter(FakeMCP([]))
    with pytest.raises(TerminalAdapterError):
        await adapter.create("production-agent", agent_type="codex", node="dell-linux", cwd="/home/dell/workspace")
    with pytest.raises(TerminalAdapterError):
        await adapter.create("harness-probe-1234", agent_type="codex", node="dell-linux", cwd="/")


def test_create_and_cleanup_require_verified_deleted_and_absence():
    asyncio.run(_test_create_and_cleanup_require_verified_deleted_and_absence())

async def _test_create_and_cleanup_require_verified_deleted_and_absence():
    name = "harness-probe-1234"
    backend = FakeMCP([
        {"status": "OK", "result": {"session": name, "state": "READY"}},
        {"status": "OK", "result": {"targets": [{"session": name, "state": "IDLE", "input_required": False}]}},
        {"status": "OK", "result": {"deleted": True, "receipt": {"tmux_absent_verified": True}}},
        {"status": "OK", "result": {"sessions": []}},
    ])
    adapter = TerminalMCPAdapter(backend)
    await adapter.create(name, agent_type="shell", node="dell-linux", cwd="/home/dell/workspace")
    assert await adapter.cleanup(name) is True
    assert backend.calls[2][1]["args"] == {"confirm": True}
    with pytest.raises(TerminalAdapterError):
        await adapter.cleanup(name)


def test_cleanup_refuses_running_without_calling_delete():
    asyncio.run(_test_cleanup_refuses_running_without_calling_delete())

async def _test_cleanup_refuses_running_without_calling_delete():
    name = "harness-probe-1234"
    backend = FakeMCP([
        {"status": "OK", "result": {"session": name, "state": "READY"}},
        {"status": "OK", "result": {"targets": [{"session": name, "state": "RUNNING"}]}},
    ])
    adapter = TerminalMCPAdapter(backend)
    await adapter.create(name, agent_type="shell", node="dell-linux", cwd="/home/dell/workspace")
    with pytest.raises(TerminalAdapterError, match="not safely idle"):
        await adapter.cleanup(name)
    assert len(backend.calls) == 2


def test_cleanup_no_absence_proof_returns_false():
    asyncio.run(_test_cleanup_no_absence_proof_returns_false())

async def _test_cleanup_no_absence_proof_returns_false():
    name = "harness-probe-1234"
    backend = FakeMCP([
        {"status": "OK", "result": {"session": name, "state": "READY"}},
        {"status": "OK", "result": {"targets": [{"session": name, "state": "IDLE"}]}},
        {"status": "OK", "result": {"deleted": True, "receipt": {"tmux_absent_verified": False}}},
    ])
    adapter = TerminalMCPAdapter(backend)
    await adapter.create(name, agent_type="shell", node="dell-linux", cwd="/home/dell/workspace")
    assert await adapter.cleanup(name) is False


def test_decode_rejects_unstructured_result():
    with pytest.raises(TerminalAdapterError):
        decode_tool_response(SimpleNamespace(is_error=False, structured_content=None,
                                             content=[SimpleNamespace(text="not-json")]))


def test_create_uses_evidence_backed_node_admission_before_dispatch():
    asyncio.run(_test_create_uses_evidence_backed_node_admission_before_dispatch())


async def _test_create_uses_evidence_backed_node_admission_before_dispatch():
    name = "harness-route-1234"
    now = datetime.now(timezone.utc).isoformat()
    nodes = lambda: [{"id": "dell-linux", "status": "online", "capacity_status": "healthy",
                      "capacity_observed_at": now, "free_bytes": 1000, "tools": {"codex": True}}]
    backend = FakeMCP([{"status": "OK", "result": {"session": name, "state": "READY"}}])
    adapter = TerminalMCPAdapter(backend, node_snapshot=nodes)
    await adapter.create(name, agent_type="codex", node="dell-linux", cwd="/home/dell/workspace", min_free_bytes=100)
    assert backend.calls[0][1]["node"] == "dell-linux"


def test_create_fails_closed_for_unverified_node_snapshot():
    asyncio.run(_test_create_fails_closed_for_unverified_node_snapshot())


async def _test_create_fails_closed_for_unverified_node_snapshot():
    adapter = TerminalMCPAdapter(FakeMCP([]), node_snapshot=lambda: [{"id": "dell-linux", "status": "online", "capacity_status": "healthy", "tools": {"codex": False}}])
    with pytest.raises(TerminalAdapterError, match="node admission rejected"):
        await adapter.create("harness-route-1234", agent_type="codex", node="dell-linux", cwd="/home/dell/workspace")
