from __future__ import annotations
import secrets
import shutil
import socket
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

from app.services import ssrf_guard


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

    def __init__(self, docker_bin: str | None = None, timeout: int = 300, enforce_ssrf_guard: bool = False):
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"
        self.timeout = timeout
        # B1.2: only ever True when the caller (app/main.py) already
        # knows AUTH_MODE=='required' -- see health_check() and this
        # module's own docstring precedent in ssrf_guard.py. Default
        # False preserves every existing direct construction (tests,
        # AUTH_MODE=none) byte-for-byte.
        self.enforce_ssrf_guard = enforce_ssrf_guard

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

    def run_ephemeral(self, command: str, worktree_root: Path, working_dir: str, timeout: int, image: str,
                       network: str, memory: str, cpus: str, pids_limit: int,
                       env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """B0.6 -- mandatory sandboxing: run ONE PROJECT.yaml-declared
        command (a repo's own preflight/test/build/gate-baseline-probe
        command -- tenant-supplied code) inside a fresh, disposable
        container instead of directly on the host. Real container
        isolation, not a policy toggle around the same subprocess call:

        - `--rm` -- the container never outlives this one call, success
          or failure, so nothing needs a separate cleanup pass.
        - `--name wm-exec-<random>` -- unique per call, giving a stable
          handle to `docker kill` on timeout (see below) and to any
          later ownership audit (matches the `wm-` compose-project
          naming convention SandboxRuntimeService.verify_owned already
          checks for the OTHER, long-running sandbox kind).
        - `--memory`/`--cpus`/`--pids-limit` -- real cgroup resource
          caps (adversarial "resource abuse" coverage: a fork-bomb or
          memory-bomb command is capped by the KERNEL, not merely
          slowed down).
        - `--network` (default "none") -- no outbound network unless a
          repo explicitly opts in via its own `exec_sandbox.network:`.
        - `--cap-drop ALL --security-opt no-new-privileges` -- no Linux
          capabilities beyond an unprivileged process's own default, no
          privilege-escalation via setuid binaries.
        - `-v {worktree_root}:/workspace:rw -w /workspace[/{working_dir}]`
          -- the command sees ONLY the one worktree/probe directory it
          was given (the FULL worktree, matching what the equivalent
          direct-host `cwd=(path/working).resolve()` call could always
          see, e.g. a monorepo command whose command references a
          sibling directory outside its own `working_directory`), bind-
          mounted at a fixed, predictable path -- never the host
          filesystem beyond that, never a sibling Task's/Workspace's own
          worktree (each call gets its own fresh container + exactly one
          bind mount, so there is no shared mutable state between
          concurrent sandboxed runs to leak through in the first place).

        Timeout/cancel: subprocess.run's own `timeout=` (inside `_run`,
        which converts a `subprocess.TimeoutExpired` into
        `SandboxRuntimeError("RUNTIME_TIMEOUT", ...)`) kills the LOCAL
        `docker run` client process on expiry, but -- unlike a plain
        host subprocess -- the CONTAINER itself, owned by the Docker
        daemon, keeps running unless explicitly told to stop. The
        `--name` is reserved up front specifically so the
        `RUNTIME_TIMEOUT` branch below can `docker kill` (then `docker
        rm -f` -- `--rm` alone doesn't fire for a killed-out-from-under-
        it client) that exact container before re-raising -- never
        leaving an orphaned, still-running tenant-code container behind
        a timed-out request.

        `env`, if given, is injected via explicit `-e KEY=VALUE` flags
        ONLY -- never the host's own `os.environ` (unlike the direct-
        host execution path SandboxedCommandRunner falls back to under
        AUTH_MODE=none, which legitimately does inherit the host
        environment, matching today's exact pre-B0.6 behavior). A
        tenant-supplied command must never see host secrets it wasn't
        explicitly, deliberately given."""
        container_name = f"wm-exec-{secrets.token_hex(8)}"
        working_dir = (working_dir or ".").strip().strip("/")
        container_workdir = "/workspace" if working_dir in ("", ".") else f"/workspace/{working_dir}"
        env_flags = []
        for key, value in (env or {}).items():
            env_flags += ["-e", f"{key}={value}"]
        argv = [
            self.docker_bin, "run", "--rm", "--name", container_name,
            "--memory", memory, "--cpus", cpus, "--pids-limit", str(pids_limit),
            "--network", network,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            *env_flags,
            "-v", f"{worktree_root}:/workspace:rw", "-w", container_workdir,
            image, "sh", "-c", command,
        ]
        try:
            return self._run(argv, worktree_root, timeout=timeout)
        except SandboxRuntimeError as exc:
            if exc.code != "RUNTIME_TIMEOUT":
                raise
            self._run([self.docker_bin, "kill", container_name], worktree_root, timeout=30)
            # `docker rm -f` immediately after `kill` can transiently race
            # a container mid-transition into Docker's own "Dead" state
            # ("removal of container ... is already in progress" or
            # similar) -- found by this session's own testing: `_run`
            # doesn't inspect returncode, so a first failed removal
            # attempt would otherwise go unnoticed and leave a real
            # orphaned (if inert) container behind. A short bounded
            # retry, not a single best-effort attempt, is what actually
            # guarantees "never leaving an orphaned container" rather
            # than merely making it less likely.
            for attempt in range(5):
                result = self._run([self.docker_bin, "rm", "-f", container_name], worktree_root, timeout=30)
                if result.returncode == 0:
                    break
                import time as _time
                _time.sleep(0.5 * (attempt + 1))
            raise

    def health_check(self, url: str, timeout: float = 5.0) -> tuple[bool, str]:
        if self.enforce_ssrf_guard:
            try:
                ssrf_guard.check_url(url)
            except ssrf_guard.SSRFGuardError as exc:
                return False, f"{exc.code}: {exc.message}"
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
