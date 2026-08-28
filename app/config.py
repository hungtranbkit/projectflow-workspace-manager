from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    host: str
    port: int
    db_path: Path
    test_timeout: int
    configured_worktree_root: Path | None = None
    agents: tuple[str, ...] = ("codex", "claude", "gemini", "aider", "other")
    default_base_branch: str = "main"
    configured_state_dir: Path | None = None
    max_running_sandboxes: int = 3
    sandbox_retention_hours: int = 24
    cleanup_poll_seconds: int = 60

    @property
    def worktree_root(self) -> Path:
        return (self.configured_worktree_root or (self.root / ".worktrees")).resolve()

    @property
    def state_dir(self) -> Path:
        return (self.configured_state_dir or Path.home() / ".local" / "state" / "projectflow-workspace-manager").resolve()


def load_settings() -> Settings:
    project_root = Path(__file__).resolve().parents[1]
    root = Path(os.getenv("WORKSPACE_MANAGER_ROOT", str(project_root.parent))).resolve()
    worktree_root = Path(os.getenv("WORKSPACE_MANAGER_WORKTREE_ROOT", str(root / ".worktrees"))).resolve()
    db_raw = Path(os.getenv("WORKSPACE_MANAGER_DB", "data/workspace-manager.db"))
    db_path = db_raw if db_raw.is_absolute() else project_root / db_raw
    state_raw = os.getenv("WORKSPACE_MANAGER_STATE_DIR")
    state_dir = Path(state_raw).resolve() if state_raw else None
    return Settings(
        root, os.getenv("WORKSPACE_MANAGER_HOST", "127.0.0.1"), int(os.getenv("WORKSPACE_MANAGER_PORT", "8765")),
        db_path.resolve(), int(os.getenv("WORKSPACE_MANAGER_TEST_TIMEOUT", "1800")), worktree_root,
        configured_state_dir=state_dir,
        max_running_sandboxes=int(os.getenv("MAX_RUNNING_SANDBOXES", "3")),
        sandbox_retention_hours=int(os.getenv("SANDBOX_RETENTION_HOURS", "24")),
        cleanup_poll_seconds=int(os.getenv("WORKSPACE_MANAGER_CLEANUP_POLL_SECONDS", "60")),
    )
