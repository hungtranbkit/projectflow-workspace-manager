"""Conservative Terminal MCP adapter for the persistent Harness state machine.

Requires an already-authorized MCP ClientSession; no credentials, no automatic
login/trust or config changes. Designed for orchestration at scheduled turns,
not a hidden client-side polling loop.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.node_router import NodeRoutingError, select_node

OWNED_NAME = re.compile(r"^harness-[a-z0-9][a-z0-9-]{3,60}$")


class TerminalAdapterError(RuntimeError):
    pass


def decode_tool_response(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False):
        raise TerminalAdapterError("MCP tool reported an error")
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and "status" in structured:
        return structured
    for part in getattr(result, "content", ()):
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict) and "status" in value:
            return value
    raise TerminalAdapterError("MCP returned no structured status")


@dataclass(frozen=True)
class DispatchEvidence:
    task_id: str
    submitted: bool
    running_verified: bool


@dataclass(frozen=True)
class Observation:
    state: str
    node: str | None
    reason: str | None
    evidence: str | None
    input_required: bool


class TerminalMCPAdapter:
    """MCP API wrapper; only sessions created by this instance may be deleted."""

    def __init__(self, client: Any, *, node_snapshot: Any = None):
        self.client = client
        self.node_snapshot = node_snapshot
        self._owned: set[str] = set()

    async def _turn(self, action: str, **kwargs: Any) -> dict[str, Any]:
        response = decode_tool_response(await self.client.call_tool(
            "terminal_turn", {"action": action, **kwargs}))
        if response.get("status") in ("FAILED", "ERROR", "DENIED"):
            reason = response.get("result", {}).get("error", "unknown")
            raise TerminalAdapterError(f"{action} failed: {reason}")
        return response

    async def create(self, name: str, *, agent_type: str, node: str, cwd: str, required_tool: str = "codex", min_free_bytes: int = 0) -> dict[str, Any]:
        if not OWNED_NAME.fullmatch(name):
            raise TerminalAdapterError("owned session must have harness-* name")
        if agent_type not in ("codex", "claude", "shell"):
            raise TerminalAdapterError("agent type not allowed")
        if not node or not cwd.startswith("/") or cwd == "/":
            raise TerminalAdapterError("explicit node and constrained cwd required")
        if self.node_snapshot is not None:
            try:
                selected = select_node(self.node_snapshot(), required_tool=required_tool,
                                       min_free_bytes=min_free_bytes, requested_node=node)
            except (NodeRoutingError, TypeError) as exc:
                raise TerminalAdapterError(f"node admission rejected: {exc}") from exc
            if str(selected.get("id") or selected.get("name")) != node:
                raise TerminalAdapterError("node admission returned a different target")
        result = await self._turn("create_session", target=name,
                                  agent_type=agent_type, node=node, working_directory=cwd)
        body = result.get("result", {})
        if result.get("status") != "OK" or body.get("session") != name:
            raise TerminalAdapterError("creation was not verified")
        # Blocked startup is NOT a usable agent, but it still belongs to this
        # runner and may be reconciled later; never auto-answer trust prompts.
        self._owned.add(name)
        return body

    async def dispatch(self, target: str, prompt: str, *, request_key: str) -> DispatchEvidence:
        if not (isinstance(prompt, str) and prompt.strip() and request_key):
            raise TerminalAdapterError("task text and idempotency key required")
        response = await self._turn("start", target=target, text=prompt, request_key=request_key)
        body = response.get("result", {})
        receipt = body.get("task_id") or body.get("submission_id") or body.get("correlation_id")
        if not receipt:
            raise TerminalAdapterError("no durable task/submission ID: not dispatched")
        # Only explicit server-verified RUNNING signal may qualify, never a
        # submission receipt or the existence of a terminal session.
        running = response.get("status") == "TASK_STARTED" and body.get("task_state") == "RUNNING"
        return DispatchEvidence(task_id=str(receipt), submitted=True, running_verified=running)

    async def observe(self, target: str) -> Observation:
        result = await self._turn("inspect", targets=[target], tail_lines=10, compact=True)
        matches = result.get("result", {}).get("targets", [])
        if len(matches) != 1 or matches[0].get("session") != target:
            raise TerminalAdapterError("session readback does not match requested target")
        body = matches[0]
        # A claimed RUNNING state still needs a fresh independent progress
        # heartbeat and process liveness before HarnessStore can mark RUNNING.
        return Observation(state=str(body.get("state", "UNKNOWN")),
                           node=body.get("node"), reason=body.get("reason"),
                           evidence=body.get("tail"), input_required=bool(body.get("input_required")))

    async def cleanup(self, name: str) -> bool:
        if name not in self._owned:
            raise TerminalAdapterError("cleanup refused: session not owned by this adapter")
        observed = await self.observe(name)
        if observed.state != "IDLE" or observed.input_required:
            raise TerminalAdapterError("cleanup refused: session not safely idle")
        result = await self._turn("delete_session", target=name, args={"confirm": True})
        body = result.get("result", {})
        receipt = body.get("receipt", {})
        deleted = body.get("deleted") is True and receipt.get("tmux_absent_verified") is True
        if not deleted:
            return False
        # Provider tombstone is checked by its own guard; also verify no
        # session with the same name remains discoverable.
        listed = await self._turn("list_sessions", compact=True)
        sessions = listed.get("result", {}).get("sessions", [])
        if any(row.get("name") == name for row in sessions):
            return False
        self._owned.remove(name)
        return True
