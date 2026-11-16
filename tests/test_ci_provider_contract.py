"""Fake provider transport never hides missing binaries or invalid responses."""
import pytest
from app.services.planner_service import PlannerAgentInvoker, PlannerAgentError


def test_missing_cli_fails_before_transport():
    def must_not_run(*args):
        raise AssertionError("transport called with no executable")
    invoker = PlannerAgentInvoker(runner=must_not_run, which=lambda name: None)
    with pytest.raises(PlannerAgentError, match="claude CLI not found"):
        invoker.invoke("claude", "prompt", {}, "/tmp")


def test_fake_transport_still_rejects_invalid_envelope():
    calls = []
    def bad_response(*args):
        calls.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "not JSON", "stderr": ""})()
    invoker = PlannerAgentInvoker(runner=bad_response, which=lambda name: "/test-only/claude")
    with pytest.raises(PlannerAgentError):
        invoker.invoke("claude", "prompt", {}, "/tmp")
    assert len(calls) == 1  # invalid structured data must not trigger retries
