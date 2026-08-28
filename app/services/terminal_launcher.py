from __future__ import annotations
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from app.launchers import AGENT_LAUNCHERS, AgentLauncher

class LauncherError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

@dataclass(frozen=True)
class TerminalAdapter:
    name: str
    executable: str

    def argv(self, worktree: Path, command: list[str] | None) -> list[str]:
        path = str(worktree)
        if self.name == "ptyxis": return [self.executable, "--new-window", "--working-directory", path, *(["--", *command] if command else [])]
        if self.name == "gnome-terminal": return [self.executable, "--window", f"--working-directory={path}", *(["--", *command] if command else [])]
        if self.name == "kgx": return [self.executable, "--working-directory", path, *(["--", *command] if command else [])]
        if self.name == "konsole": return [self.executable, "--workdir", path, *(["-e", *command] if command else [])]
        if self.name == "xfce4-terminal":
            return [self.executable, f"--working-directory={path}", *(["--command", shlex.join(command)] if command else [])]
        if self.name == "xterm": return [self.executable, *(["-e", *command] if command else [])]
        raise LauncherError("TERMINAL_UNSUPPORTED", f"Unsupported terminal adapter: {self.name}")

class TerminalLauncherService:
    TERMINALS = ("ptyxis", "gnome-terminal", "kgx", "konsole", "xfce4-terminal", "xterm")

    def __init__(self, settings, git_service, launchers=None, which=shutil.which, popen=subprocess.Popen, environ=None):
        self.settings, self.git = settings, git_service
        self.launchers = launchers or AGENT_LAUNCHERS
        self.which, self.popen = which, popen
        self.environ = environ if environ is not None else os.environ

    def launcher_for(self, agent: str) -> AgentLauncher:
        launcher = self.launchers.get(agent.lower())
        if not launcher: raise LauncherError("AGENT_UNSUPPORTED", f"Chưa cấu hình launcher cho agent: {agent}")
        return launcher

    def validate_worktree(self, value: str | Path) -> Path:
        try: path = self.git.validate_worktree(value)
        except Exception as exc: raise LauncherError("INVALID_WORKTREE", "Worktree nằm ngoài vùng .worktrees được phép") from exc
        if not path.is_dir(): raise LauncherError("WORKTREE_NOT_FOUND", "Worktree không còn tồn tại")
        try: self.git.validate_repo(path)
        except Exception as exc: raise LauncherError("INVALID_WORKTREE", "Path không phải Git worktree hợp lệ") from exc
        return path

    def detect_terminal(self) -> TerminalAdapter:
        if not (self.environ.get("DISPLAY") or self.environ.get("WAYLAND_DISPLAY")):
            raise LauncherError("DESKTOP_SESSION_UNAVAILABLE", "Service không chạy trong desktop session")
        for name in self.TERMINALS:
            executable = self.which(name)
            if executable: return TerminalAdapter(name, executable)
        raise LauncherError("TERMINAL_NOT_FOUND", "Không tìm thấy terminal emulator được hỗ trợ")

    def status(self):
        terminal = None
        try: terminal = self.detect_terminal().name
        except LauncherError: pass
        return [{"agent": key, "label": value.label, "executable": self.which(value.executable), "args": list(value.args), "available": bool(self.which(value.executable)), "source": value.source, "terminal": terminal} for key,value in self.launchers.items()]

    def open_terminal(self, worktree): return self._launch(worktree, None, None)

    def launch_agent(self, worktree, agent: str):
        launcher = self.launcher_for(agent)
        executable = self.which(launcher.executable)
        if not executable: raise LauncherError("AGENT_CLI_NOT_FOUND", f"{launcher.label} CLI chưa được cài hoặc không có trong PATH")
        return self._launch(worktree, launcher.label, [executable, *launcher.args])

    def _launch(self, worktree, label, command):
        path = self.validate_worktree(worktree); terminal = self.detect_terminal(); argv = terminal.argv(path, command)
        try: self.popen(argv, cwd=path, env=dict(self.environ), start_new_session=True, close_fds=True)
        except OSError as exc: raise LauncherError("LAUNCH_FAILED", f"Không mở được terminal: {exc}") from exc
        return {"terminal": terminal.name, "worktree": str(path), "agent": label, "result": "requested"}
