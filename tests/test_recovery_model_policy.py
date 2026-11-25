from __future__ import annotations

import json

import pytest

from app.services.planner_service import PlannerAgentError, PlannerAgentInvoker
from app.services.recovery_decision_service import RecoveryDecisionService


def test_planner_invoker_pins_model_effort_and_returns_attested_receipt(tmp_path):
    seen = {}

    def runner(argv, cwd, timeout):
        seen["argv"] = argv

        class Result:
            returncode = 0
            stdout = json.dumps({
                "is_error": False,
                "subtype": "success",
                "result": '{"action":"VERIFY"}',
                "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}},
                "num_turns": 2,
                "total_cost_usd": 0.0,
            })
            stderr = ""

        return Result()

    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/claude")
    receipt = invoker.invoke("claude", "bounded probe", {"type": "object"}, tmp_path,
                             model="claude-opus-5-5", effort="high", return_receipt=True)

    assert receipt["resolved_model"] == "claude-opus-5-5"
    assert receipt["effort_requested"] == "high"
    assert receipt["text"] == '{"action":"VERIFY"}'
    assert seen["argv"][-4:] == ["--model", "claude-opus-5-5", "--effort", "high"]


def test_planner_invoker_refuses_unattested_requested_model(tmp_path):
    def runner(argv, cwd, timeout):
        class Result:
            returncode = 0
            stdout = json.dumps({"is_error": False, "subtype": "success", "result": "{}", "modelUsage": {}})
            stderr = ""
        return Result()

    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/claude")
    with pytest.raises(PlannerAgentError, match="did not attest"):
        invoker.invoke("claude", "bounded probe", {"type": "object"}, tmp_path,
                      model="claude-opus-5-5", effort="high", return_receipt=True)


def test_toolless_recovery_uses_no_schema_tool_and_rejects_tool_activity(tmp_path):
    seen = {}

    def runner(argv, cwd, timeout):
        seen["argv"] = argv
        class Result:
            returncode = 0
            stdout = json.dumps({"is_error": False, "subtype": "success", "result": '{"ok":true}',
                "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}},
                "num_turns": 1, "stop_reason": "end_turn", "permission_denials": [],
                "usage": {"server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}}})
            stderr = ""
        return Result()

    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/claude")
    receipt = invoker.invoke("claude", "sanitized incident", {"type": "object"}, tmp_path,
        model="claude-opus-5-5", effort="high", return_receipt=True, require_toolless=True)
    assert "--tools" in seen["argv"] and seen["argv"][seen["argv"].index("--tools") + 1] == ""
    assert "--json-schema" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("--max-turns") + 1] == "1"
    assert receipt["toolless_verified"] is True

    def tool_using_runner(argv, cwd, timeout):
        class Result:
            returncode = 0
            stdout = json.dumps({"is_error": False, "subtype": "success", "result": '{"ok":true}',
                "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}},
                "num_turns": 2, "stop_reason": "tool_use", "permission_denials": [],
                "usage": {"server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}}})
            stderr = ""
        return Result()
    invoker.runner = tool_using_runner
    with pytest.raises(PlannerAgentError, match="tool activity"):
        invoker.invoke("claude", "sanitized incident", {"type": "object"}, tmp_path,
            model="claude-opus-5-5", effort="high", return_receipt=True, require_toolless=True)

    def unreceipted_runner(argv, cwd, timeout):
        class Result:
            returncode = 0
            stdout = json.dumps({"is_error": False, "subtype": "success", "result": '{"ok":true}',
                "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}},
                "num_turns": 1, "stop_reason": "end_turn", "permission_denials": []})
            stderr = ""
        return Result()
    invoker.runner = unreceipted_runner
    with pytest.raises(PlannerAgentError, match="tool activity"):
        invoker.invoke("claude", "sanitized incident", {"type": "object"}, tmp_path,
            model="claude-opus-5-5", effort="high", return_receipt=True, require_toolless=True)


def test_claude_effort_is_allowlisted(tmp_path):
    invoker = PlannerAgentInvoker(runner=lambda *args: None, which=lambda name: "/usr/bin/claude")
    with pytest.raises(PlannerAgentError, match="effort"):
        invoker.invoke("claude", "bounded probe", {"type": "object"}, tmp_path,
                      model="claude-opus-5-5", effort="xhigh")


def test_codex_recovery_request_uses_read_only_schema_and_requires_model_receipt(tmp_path):
    seen = {}

    def runner(argv, cwd, timeout, *, input_text=None):
        seen.update(argv=argv, cwd=cwd, input=input_text)
        output_path = argv[argv.index("--output-last-message") + 1]
        open(output_path, "w").write('{"action":"VERIFY"}')

        class Result:
            returncode = 0
            stdout = '{"type":"thread.started","thread_id":"safe"}\n{"type":"turn.completed","usage":{"input_tokens":8}}\n'
            stderr = ""
        return Result()

    invoker = PlannerAgentInvoker(runner=runner, which=lambda name: "/usr/bin/codex")
    with pytest.raises(PlannerAgentError, match="no supported per-call switch"):
        invoker.invoke("codex", "sanitized incident", {"type": "object"}, tmp_path,
                       model="gpt-6.2-astra", effort="high", return_receipt=True)
    assert seen == {}  # Never start a Codex session that can execute shell/MCP tools.
    assert PlannerAgentInvoker.advisory_toolless_capable("codex") is False
    assert PlannerAgentInvoker.advisory_toolless_capable("claude") is True


def test_recovery_advice_is_sanitized_validated_and_idempotently_cached(client, tmp_path):
    refs = ["change:5", "task:9", "session:12"]
    decision = {"incident_id": "", "evidence_refs": refs, "cause": "ambiguous liveness",
                "confidence": 0.8, "allowed_action": "VERIFY", "owner": "Change owner",
                "target": "session:12", "checkpoint": "preserve current worktree",
                "risk": "NORMAL", "expected_verification": "fresh process evidence",
                "stop_condition": "ownership unknown", "rollback": "no mutation made"}

    class Invoker:
        calls = 0
        def model_supported(self, provider, model, effort):
            return provider == "codex" and model == "gpt-6.2-astra" and effort == "high"
        def invoke(self, provider, prompt, schema, cwd, **kwargs):
            self.calls += 1
            self.prompt = prompt
            assert provider == "claude"
            assert kwargs["model"] == "claude-opus-5-5"
            assert kwargs["effort"] == "high"
            assert kwargs["max_attempts"] == 1
            assert kwargs["require_toolless"] is True
            decision["incident_id"] = json.loads(prompt[prompt.index('{'):])["incident_id"]
            return {"text": json.dumps(decision), "provider": "claude",
                    "resolved_model": "claude-opus-5-5", "effort_requested": "high",
                    "turns": 2, "cost_estimate_usd": 0.1, "toolless_verified": True}

    invoker = Invoker()
    advisor = RecoveryDecisionService(client.app.state.db, invoker, tmp_path,
                                      provider_available=lambda provider: provider == "claude")
    sample_key = "sk-" + "ABCD0123" * 4
    incident = {"task_id": 9, "failure": "SESSION_LIVENESS_UNKNOWN", "epoch": 12,
                "task_goal": f"check code {sample_key}"}
    first = advisor.advise(incident, refs)
    second = advisor.advise(incident, refs)

    assert first["source"] == "MODEL_ADVISORY"
    assert first["allowed_action"] == "VERIFY"
    assert first["provider_receipt"]["resolved_model"] == "claude-opus-5-5"
    assert second["source"] == "CACHED_MODEL_PROPOSAL"
    assert invoker.calls == 1
    assert sample_key not in invoker.prompt
    assert "***REDACTED***" in invoker.prompt
    assert client.app.state.db.one("SELECT COUNT(*) c FROM agent_sessions WHERE task_id=9")["c"] == 0


def test_recovery_advice_rejects_wrong_evidence_refs_and_falls_back(client, tmp_path):
    class Invoker:
        def model_supported(self, provider, model, effort):
            return provider == "codex" and model == "gpt-6.2-astra" and effort == "high"
        def invoke(self, provider, prompt, schema, cwd, **kwargs):
            packet = json.loads(prompt[prompt.index('{'):])
            return {"text": json.dumps({"incident_id": packet["incident_id"],
                "evidence_refs": ["task:attacker"], "cause": "x", "confidence": .5,
                "allowed_action": "RETRY_SAFE_IDEMPOTENT", "owner": "Change owner", "target": "task:1",
                "checkpoint": "x", "risk": "UNKNOWN", "expected_verification": "x",
                "stop_condition": "x", "rollback": "x"}), "provider": "claude",
                "resolved_model": "claude-opus-5-5", "effort_requested": "high", "turns": 1,
                "cost_estimate_usd": 0.1, "toolless_verified": True}

    advisor = RecoveryDecisionService(client.app.state.db, Invoker(), tmp_path,
                                      provider_available=lambda provider: provider == "claude")
    result = advisor.advise({"task_id": 1, "failure": "UNKNOWN", "epoch": 2}, ["task:1"])
    assert result == {"source": "DETERMINISTIC_FALLBACK", "allowed_action": "VERIFY",
                      "reason": "AI_PROPOSAL_INVALID"}
    assert client.app.state.db.one("SELECT COUNT(*) c FROM workspace_events WHERE action='RECOVERY_AI_PROPOSAL'")["c"] == 0


def test_codex_advisory_is_skipped_when_tools_cannot_be_disabled_and_opus_is_receipted(client, tmp_path):
    calls = []
    refs = ["task:22", "session:8"]

    class Invoker:
        def model_supported(self, provider, model, effort):
            return (provider, model, effort) == ("codex", "gpt-6.2-astra", "high")

        def advisory_toolless_capable(self, provider):
            return provider == "claude"

        def invoke(self, provider, prompt, schema, cwd, **kwargs):
            calls.append((provider, kwargs["model"], kwargs["effort"]))
            if provider == "codex":
                raise PlannerAgentError("Codex provider did not supply matching effective model receipt")
            packet = json.loads(prompt[prompt.index('{'):])
            result = {"incident_id": packet["incident_id"], "evidence_refs": refs,
                "cause": "dispatch receipt is ambiguous", "confidence": 0.9,
                "allowed_action": "VERIFY", "owner": "Change owner", "target": "session:8",
                "checkpoint": "preserve task and branch", "risk": "NORMAL",
                "expected_verification": "observe the existing process", "stop_condition": "unknown owner",
                "rollback": "no mutation made"}
            return {"text": json.dumps(result), "provider": "claude", "resolved_model": "claude-opus-5-5",
                    "effort_requested": "high", "turns": 2, "cost_estimate_usd": .2,
                    "toolless_verified": True}

    advisor = RecoveryDecisionService(client.app.state.db, Invoker(), tmp_path,
                                      provider_available=lambda _provider: True)
    result = advisor.advise({"task_id": 22, "failure": "UNKNOWN", "epoch": 8,
                             "owner": "Change owner"}, refs)
    assert calls == [("claude", "claude-opus-5-5", "high")]
    assert result["source"] == "MODEL_ADVISORY"
    assert result["provider_receipt"]["resolved_model"] == "claude-opus-5-5"
    audit = client.app.state.db.all("SELECT action,details FROM workspace_events WHERE action IN "
        "('RECOVERY_AI_CANDIDATE_UNVERIFIED','RECOVERY_AI_PROPOSAL') ORDER BY id")
    assert len(audit) == 1
    assert "claude-opus-5-5" in audit[0]["details"]
