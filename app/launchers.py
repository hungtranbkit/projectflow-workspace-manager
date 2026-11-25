from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class AgentLauncher:
    label: str
    executable: str
    args: tuple[str, ...]
    source: str = "built-in"
    completion_mode: str = "PTY_INTERACTIVE"

AGENT_LAUNCHERS = {
    # V2 Codex tasks use the supported non-interactive final-message file
    # contract; V1/legacy sessions continue to use the interactive PTY.
    "codex": AgentLauncher("Codex", "codex", ("--yolo",), completion_mode="CODEX_EXEC"),
    "claude": AgentLauncher("Claude", "claude", ("--dangerously-skip-permissions",)),
}
