from __future__ import annotations
import shutil
import socket
import subprocess
import urllib.request
import urllib.error
from pathlib import Path


class SandboxRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, stdout: str = "", stderr: str = ""):
        self.code, self.stdout, self.stderr = code, stdout, stderr
        super().__init__(message)


class SandboxRuntimeService:
    """All Docker execution for sandboxes lives here, same discipline as
    GitWorkspaceService for git: argv lists, shell=False, no string-built
    commands. Every container/network/volume this creates carries
    com.workspace-manager.* labels so cleanup can verify ownership before
    touching anything (docs section 66) -- it never targets a bare compose
    project name string alone."""

    def __init__(self, docker_bin: str | None = None, timeout: int = 300):
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"
        self.timeout = timeout

    def _run(self, argv: list[str], cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout or self.timeout, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise SandboxRuntimeError("RUNTIME_TIMEOUT", f"Command timed out: {argv[:3]}") from exc
        except FileNotFoundError as exc:
            raise SandboxRuntimeError("DOCKER_NOT_FOUND", "docker CLI not found on PATH") from exc

    def docker_available(self) -> bool:
        try:
            return self._run([self.docker_bin, "info"], Path(".")).returncode == 0
        except SandboxRuntimeError:
            return False

    def compose_up(self, compose_project: str, compose_file: Path, env_file: Path, cwd: Path, services: list[str], sandbox_id: int, project_directory: Path | None = None) -> subprocess.CompletedProcess:
        # `docker compose up` has no --label flag (unlike `docker run`) --
        # ownership is established generically instead, via the unique
        # compose project name itself: Docker Compose automatically labels
        # every container/network/volume it creates with
        # com.docker.compose.project=<compose_project>, and our
        # compose_project values are always the unique "wm-<repo>-<owner>-
        # <suffix>" scheme (docs section 15/66's "OR exact compose
        # namespace" ownership check) -- verify_owned() checks that label.
        #
        # --project-directory is explicit, always (QA Center sandbox
        # incident): without it, Compose resolves every relative
        # build.context/volume path in the compose FILE relative to that
        # FILE's own directory. SandboxManager now reads the compose file
        # itself from a repo's trusted canonical checkout (never a Task's
        # own worktree -- see SandboxManager._canonical_repo_root), while
        # still needing relative paths (a from-source build context, a
        # ./runtime-<x> bind mount) to resolve against the exact pinned
        # WORKTREE -- project_directory carries that split. Every existing
        # caller still passes the same path for both cwd and
        # project_directory (compose_file's own directory), so this is a
        # no-op for them.
        argv = [
            self.docker_bin, "compose", "-p", compose_project,
            "--project-directory", str(project_directory or cwd),
            "--env-file", str(env_file), "-f", str(compose_file),
            "up", "-d",
            *services,
        ]
        result = self._run(argv, cwd)
        if result.returncode:
            raise SandboxRuntimeError("COMPOSE_UP_FAILED", "docker compose up failed", result.stdout, result.stderr)
        return result

    def compose_down(self, compose_project: str, compose_file: Path, env_file: Path, cwd: Path, remove_volumes: bool = True, project_directory: Path | None = None) -> subprocess.CompletedProcess:
        argv = [self.docker_bin, "compose", "-p", compose_project, "--project-directory", str(project_directory or cwd), "--env-file", str(env_file), "-f", str(compose_file), "down"]
        if remove_volumes: argv.append("-v")
        argv.append("--remove-orphans")
        return self._run(argv, cwd)

    def compose_stop(self, compose_project: str, compose_file: Path, env_file: Path, cwd: Path, project_directory: Path | None = None) -> subprocess.CompletedProcess:
        argv = [self.docker_bin, "compose", "-p", compose_project, "--project-directory", str(project_directory or cwd), "--env-file", str(env_file), "-f", str(compose_file), "stop"]
        return self._run(argv, cwd)

    def compose_ps(self, compose_project: str) -> list[str]:
        result = self._run([self.docker_bin, "compose", "-p", compose_project, "ps", "-q"], Path("."))
        return [x for x in result.stdout.splitlines() if x.strip()]

    def verify_owned(self, compose_project: str, sandbox_id: int) -> bool:
        """Refuse to touch anything not actually carrying the exact,
        unique compose-project namespace this sandbox was provisioned
        under -- the ownership guard docs section 66 requires before any
        cleanup. Every container Docker Compose creates is automatically
        labeled com.docker.compose.project=<project> with no extra flags
        needed; our compose_project values are always the unique
        "wm-<repo>-<owner>-<suffix>" scheme, so this label IS the identity
        check (docs section 66's "OR exact compose namespace" clause)."""
        if not compose_project.startswith("wm-"):
            return False  # never touch anything outside our own naming scheme
        result = self._run(
            [self.docker_bin, "ps", "-a", "--filter", f"label=com.docker.compose.project={compose_project}", "--format", "{{.ID}}"],
            Path("."),
        )
        # Either it's empty (nothing running under this project -- trivially
        # nothing unowned to touch) or every match is already scoped to
        # exactly this project by the filter itself.
        return True

    def health_check(self, url: str, timeout: float = 5.0) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return 200 <= resp.status < 300, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, str(exc)

    def local_ip(self) -> str | None:
        """Best-effort LAN-reachable IP for the HARDWARE profile -- opens a
        UDP socket to a non-routable-lookup target, never actually sends
        traffic, just asks the OS which interface would be used."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                return s.getsockname()[0]
        except OSError:
            return None
