"""Test-only isolation for Claude Code trust state.

Real-provider tests must never edit the operator's home configuration. The
provider inherits ``CLAUDE_CONFIG_DIR`` from the test process (including the
real PTY path in AgentSessionManager), while this helper stores only the
synthetic project-trust entry inside the pytest-owned temporary root.
Authentication is supplied only by an already-authorized environment
mechanism; this module never copies or reads credentials.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
_OWNER_MARKER = ".projectflow-test-owned"
_OWNER_MARKER_CONTENT = "projectflow pytest-owned Claude config v1\n"
_TRUST_ENTRY = {
    "allowedTools": [],
    "mcpContextUris": [],
    "mcpServers": {},
    "enabledMcpjsonServers": [],
    "disabledMcpjsonServers": [],
    "hasTrustDialogAccepted": True,
    "hasClaudeMdExternalIncludesApproved": False,
    "hasClaudeMdExternalIncludesWarningShown": False,
}
_MISSING = object()


@dataclass
class ProjectTrust:
    config_file: Path
    project_key: str
    previous: object
    _closed: bool = False

    def restore(self) -> None:
        """Restore only this fixture's key; preserve every sibling entry."""
        if self._closed:
            return
        data = _read_test_config(self.config_file)
        projects = data.setdefault("projects", {})
        if self.previous is _MISSING:
            projects.pop(self.project_key, None)
        else:
            projects[self.project_key] = self.previous
        _write_json_atomically(self.config_file, data)
        self._closed = True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_fixture_root(root: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(root)))
    resolved = lexical.resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    home = Path.home().resolve(strict=True)
    if lexical != resolved:
        raise ValueError("pytest config root must not contain symlinks")
    if resolved == home or _is_within(resolved, home) or _is_within(home, resolved):
        raise ValueError("Claude test config must not overlap the user home")
    if not resolved.is_dir() or not _is_within(resolved, temp_root):
        raise ValueError("Claude test config must be inside the pytest temporary root")
    if resolved.stat().st_uid != os.getuid():
        raise ValueError("Claude test config root must be owned by the current test user")
    return resolved


def _validate_project_path(path: Path, allowed_root: Path) -> str:
    root = _validate_fixture_root(allowed_root)
    lexical = Path(os.path.abspath(os.fspath(path)))
    cursor = lexical
    while cursor != root and _is_within(cursor, root):
        if cursor.is_symlink():
            raise ValueError("Claude trust path must not contain symlinks")
        cursor = cursor.parent
    resolved = lexical.resolve(strict=False)
    if not _is_within(resolved, root):
        raise ValueError("Claude trust path must stay within this test's disposable root")
    if resolved == root:
        raise ValueError("Claude trust path must identify a fixture project below its root")
    return str(resolved)


def _read_test_config(config_file: Path) -> dict:
    if config_file.is_symlink():
        raise ValueError("test Claude config file must not be a symlink")
    try:
        info = config_file.stat(follow_symlinks=False)
    except FileNotFoundError:
        return {"projects": {}}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("test Claude config must be a regular file owned by the current test user")
    data = json.loads(config_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
        raise ValueError("test Claude config has an invalid projects map")
    return data


def _write_json_atomically(path: Path, data: dict) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def prepare_claude_test_config(allowed_root: str | Path, *, monkeypatch) -> Path:
    """Create the isolated config root before any provider subprocess starts."""
    root = _validate_fixture_root(Path(allowed_root))
    config_dir = root / ".projectflow-claude-test-config"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    if config_dir.is_symlink() or config_dir.resolve(strict=True) != config_dir:
        raise ValueError("Claude test config directory must not contain symlinks")
    info = config_dir.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Claude test config directory must be owned by the current test user")
    marker = config_dir / _OWNER_MARKER
    if marker.is_symlink():
        raise ValueError("Claude config ownership marker must not be a symlink")
    if marker.exists():
        marker_info = marker.stat(follow_symlinks=False)
        if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.getuid():
            raise ValueError("Claude config ownership marker is not a regular test-owned file")
        if marker.read_text(encoding="utf-8") != _OWNER_MARKER_CONTENT:
            raise ValueError("Claude config ownership marker is invalid")
    else:
        marker.write_text(_OWNER_MARKER_CONTENT, encoding="utf-8")
        marker.chmod(0o600)
    monkeypatch.setenv(CLAUDE_CONFIG_DIR_ENV, str(config_dir))
    return config_dir


def require_isolated_claude_auth(config_dir: str | Path) -> None:
    """Fail before any provider invocation when the isolated root is unauthenticated.

    The probe is local CLI auth status only. Credential environment values
    are never read for contents or logged. A credential variable alone is
    not evidence that this isolated configuration is authenticated.
    """
    root = _validate_fixture_root(Path(config_dir))
    if os.environ.get(CLAUDE_CONFIG_DIR_ENV) != str(root):
        raise RuntimeError("Claude test auth probe requires the matching isolated CLAUDE_CONFIG_DIR")
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("real Claude test requires the Claude CLI; no provider was invoked")
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            env=os.environ.copy(), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("isolated Claude auth status could not be checked; no provider was invoked") from exc
    try:
        status = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        status = {}
    if result.returncode != 0 or status.get("loggedIn") is not True:
        raise RuntimeError("isolated Claude config is unauthenticated; real provider was not invoked")


def register_project_trust(
    project_path: str | Path,
    *,
    allowed_root: str | Path,
    monkeypatch,
    add_finalizer: Callable[[Callable[[], None]], None],
) -> ProjectTrust:
    """Add one trust key under a unique, pytest-owned config directory.

    ``CLAUDE_CONFIG_DIR`` is set before the test launches any provider, so
    subprocesses and the real PTY child inherit exactly this isolated root.
    A finalizer is registered immediately after the write, including when a
    later launch or assertion fails.
    """
    root = _validate_fixture_root(Path(allowed_root))
    key = _validate_project_path(Path(project_path), root)
    config_dir = prepare_claude_test_config(root, monkeypatch=monkeypatch)
    config_file = config_dir / ".claude.json"
    data = _read_test_config(config_file)
    previous = data.setdefault("projects", {}).get(key, _MISSING)
    data["projects"][key] = dict(_TRUST_ENTRY)
    _write_json_atomically(config_file, data)
    registration = ProjectTrust(config_file=config_file, project_key=key, previous=previous)
    add_finalizer(registration.restore)
    return registration
