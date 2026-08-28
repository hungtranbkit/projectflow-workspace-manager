from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class AgentLauncher:
    label: str
    executable: str
    args: tuple[str, ...]
    source: str = "built-in"

AGENT_LAUNCHERS = {
    "codex": AgentLauncher("Codex", "codex", ("--yolo",)),
    "claude": AgentLauncher("Claude", "claude", ("--dangerously-skip-permissions",)),
}
