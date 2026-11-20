"""Regressions for Claude Code structured-output CLI turn limits and safe errors.

These tests invoke a fake subprocess runner; no Claude account, network, or
external model is used.
"""
import json
from types import SimpleNamespace

import pytest

from app.services.planner_service import PlannerAgentError, PlannerAgentInvoker


def test_structured_schema_uses_bounded_three_turns_and_no_tools():
    seen = []

    def runner(argv, cwd, timeout):
        seen.append((argv, cwd, timeout))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "result", "subtype": "success",
                               "is_error": False, "result": '{"ok": true}'}),
            stderr="",
        )

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/claude")
    assert json.loads(invoker.invoke("claude", "Read-only structured test", schema, "/tmp")) == {"ok": True}
    assert len(seen) == 1
    argv, cwd, timeout = seen[0]
    assert argv[argv.index("--max-turns") + 1] == "3"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema
    assert cwd == "/tmp"
    assert timeout == invoker.timeout


@pytest.mark.parametrize("subtype", [
    "error_max_turns", "error_max_structured_output_retries", "error_during_execution"
])
def test_nonzero_provider_envelope_reports_safe_subtype(subtype):
    secret = "ghp_THIS_IS_A_TEST_ONLY_SECRET_NOT_FOR_LOGGING"

    def runner(argv, cwd, timeout):
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"type": "result", "subtype": subtype, "is_error": True,
                               "errors": [secret]}),
            stderr="",
        )

    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/claude",
                                  max_attempts=1)
    with pytest.raises(PlannerAgentError) as exc:
        invoker.invoke("claude", "prompt", {"type": "object"}, "/tmp")
    assert subtype in str(exc.value)
    assert secret not in str(exc.value)
    assert "attempt 1/1" in str(exc.value)


def test_zero_exit_provider_error_does_not_expose_raw_errors():
    secret = "sk-DO_NOT_PRINT_THIS_TEST_SECRET_ANYWHERE"

    def runner(argv, cwd, timeout):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "result", "subtype": "error_max_turns",
                               "is_error": True, "stop_reason": "tool_use",
                               "errors": [secret]}),
            stderr="",
        )

    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/claude",
                                  max_attempts=1)
    with pytest.raises(PlannerAgentError) as exc:
        invoker.invoke("claude", "prompt", {"type": "object"}, "/tmp")
    assert "error_max_turns" in str(exc.value)
    assert secret not in str(exc.value)


@pytest.mark.parametrize("payload", [
    '[]',
    '{"subtype": [], "is_error": true, "errors": ["unsafe"]}',
])
def test_unexpected_provider_envelope_shape_fails_closed(payload):
    def runner(argv, cwd, timeout):
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/claude",
                                  max_attempts=1)
    with pytest.raises(PlannerAgentError):
        invoker.invoke("claude", "prompt", {"type": "object"}, "/tmp")
