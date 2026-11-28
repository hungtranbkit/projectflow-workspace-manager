import asyncio

import pytest

from app.services.agent_harness_terminal import TerminalMCPAdapter, TerminalAdapterError
from tests.test_agent_harness_terminal import FakeMCP, eligible_snapshot
from tests.test_b05_transactional_admission import _sandbox


def test_periodic_cleanup_preserves_a_fresh_claimed_provision(client, monkeypatch):
    db = client.app.state.db
    manager = client.app.state.sandboxes
    sandbox = _sandbox(db, "b05-live-claim")
    op_id = manager._admit_provision(sandbox)
    original_one = db.one

    class StopBeforeRuntime(Exception):
        pass

    def probe(sql, params=()):
        if "FROM sandbox_sources" in sql:
            assert original_one("SELECT status FROM sandboxes WHERE id=?", (sandbox,))["status"] == "STARTING"
            client.app.state.cleanup_worker.reconcile()
            assert original_one("SELECT status FROM sandboxes WHERE id=?", (sandbox,))["status"] == "STARTING"
            assert manager._admit_provision(sandbox) is None
            raise StopBeforeRuntime
        return original_one(sql, params)

    monkeypatch.setattr(db, "one", probe)
    with pytest.raises(StopBeforeRuntime):
        manager._provision_worker(sandbox, op_id)


def test_terminal_agent_must_match_capability_even_with_tool_override():
    name = "harness-route-1234"
    client = FakeMCP([{"status": "OK", "result": {"session": name}}])
    adapter = TerminalMCPAdapter(client, node_snapshot=eligible_snapshot())
    with pytest.raises(TerminalAdapterError):
        asyncio.run(adapter.create(name, agent_type="claude", node="dell-linux",
                                   cwd="/home/dell/workspace", required_tool="codex"))
    assert not client.calls


def test_periodic_cleanup_still_recovers_an_expired_queued_provision(client):
    db = client.app.state.db
    manager = client.app.state.sandboxes
    sandbox = _sandbox(db, "b05-expired-claim")
    expired = manager._admit_provision(sandbox)
    db.execute("UPDATE sandbox_operations SET started_at=datetime('now','-2 hours') WHERE id=?", (expired,))
    client.app.state.cleanup_worker.reconcile()
    assert db.one("SELECT status FROM sandboxes WHERE id=?", (sandbox,))["status"] == "UNHEALTHY"
    replacement = manager._admit_provision(sandbox)
    assert replacement is not None and replacement != expired
    manager._provision_worker(sandbox, expired)
    assert db.one("SELECT status FROM sandboxes WHERE id=?", (sandbox,))["status"] == "PROVISIONING"
