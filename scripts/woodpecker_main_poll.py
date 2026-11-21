#!/usr/bin/env python3
"""ProjectFlow trusted-main CI poller: scheduled Woodpecker CLI local backend.

STRICT SCOPE: public hungtranbkit/projectflow-workspace-manager protected main,
NOT unreviewed PRs. Woodpecker's Local backend is UNSANDBOXED; run this only
on a trusted dedicated user with no production/cloud/agent credentials.
Never interprets remote branch names or arbitrary user-supplied commands.

Run this one-shot program through a 15-minute systemd user timer.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

REPO = "hungtranbkit/projectflow-workspace-manager"
REMOTE = "https://github.com/" + REPO + ".git"
REF = "refs/heads/main"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ROOT = Path.home() / ".local" / "state" / "projectflow-woodpecker-main-ci"
APP = Path.home() / ".local" / "share" / "projectflow-woodpecker-main-ci"
CLI = Path.home() / ".local" / "share" / "projectflow-woodpecker-staging" / "v3.20.1" / "woodpecker-cli"
# CI tests must match GitHub Actions setup-python 3.11.
PROFILE_VERSION = "py311-trusted-main-fast-20261121-v3"
PYTHON311 = APP / "python311"
VENV = APP / "venv-py311"
RETRY_SECONDS = 3600
MAX_ATTEMPTS_SAME_SHA = 3
JOB_TIMEOUT_SECONDS = 900
MAX_LOG_BYTES = 2_500_000


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def policy_identity() -> str:
    """A changed trusted pipeline cannot reuse an older CI PASS."""
    pipeline = APP / "trusted-main.yaml"
    if not pipeline.is_file():
        return PROFILE_VERSION + "-MISSING"
    return PROFILE_VERSION + "-" + hashlib.sha256(pipeline.read_bytes()).hexdigest()[:12]


def stale_running(previous: dict, now_epoch: float) -> bool:
    if previous.get("status") != "RUNNING":
        return False
    try:
        age = now_epoch - float(previous.get("last_attempt_epoch", now_epoch))
    except (TypeError, ValueError):
        return False
    return age > JOB_TIMEOUT_SECONDS + 120


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def run_simple(command: list[str], *, timeout: int = 30, cwd: Path | None = None) -> str:
    p = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=timeout, check=True, cwd=cwd,
                       env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                            "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8"})
    return p.stdout.strip()


def remote_sha() -> str:
    lines = run_simple(["git", "ls-remote", "--exit-code", "--heads", REMOTE, REF], timeout=25).splitlines()
    if len(lines) != 1:
        raise ValueError("UNKNOWN_REMOTE_MAIN_SHA")
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != REF or not SHA_PATTERN.fullmatch(fields[0]):
        raise ValueError("INVALID_REMOTE_MAIN_SHA")
    return fields[0]


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, record: dict) -> None:
    tmp = path.with_name(path.name + ".new")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)


def should_run(sha: str, previous: dict, now_epoch: float, *, force_retry: bool = False) -> tuple[bool, str, int]:
    if previous.get("sha") != sha:
        return True, "NEW_SHA", 1
    if previous.get("status") in ("PASS", "FAST_PASS"):
        return False, "ALREADY_PASSED", int(previous.get("attempts", 1))
    attempts = int(previous.get("attempts", 0))
    if attempts >= MAX_ATTEMPTS_SAME_SHA:
        return False, "RETRY_LIMIT_REACHED", attempts
    last_epoch = float(previous.get("last_attempt_epoch", 0))
    if not force_retry and now_epoch - last_epoch < RETRY_SECONDS:
        return False, "RETRY_COOLDOWN", attempts
    return True, "RETRY", attempts + 1


def child_environment() -> dict[str, str]:
    """Scrub provider/API credentials from process environment."""
    private_directory(ROOT / "sandbox-home")
    private_directory(ROOT / "cache")
    private_directory(ROOT / "tmp")
    env = {
        "HOME": str(ROOT / "sandbox-home"),
        "PATH": str(VENV / "bin") + ":/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "VIRTUAL_ENV": str(VENV),
        "XDG_CACHE_HOME": str(ROOT / "cache"),
        "PIP_CACHE_DIR": str(ROOT / "cache" / "pip"),
        "TMPDIR": str(ROOT / "tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTEST_ADDOPTS": "--maxfail=1 --tb=short --disable-warnings",
    }
    return env


def logged_command(command: list[str], log, *, cwd: Path, env: dict[str, str],
                   timeout: int) -> None:
    """Bounded streaming logs; always drain process output to prevent deadlocks.

    Never print credentials. Errors propagate as failures, never implicit PASS.
    """
    log.write("$ " + " ".join(command[:3]) + " [arguments omitted]\n")
    log.flush()
    proc = subprocess.Popen(command, cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    assert proc.stdout is not None
    remaining = max(0, MAX_LOG_BYTES - log.tell())

    def drain() -> None:
        nonlocal remaining
        truncated = False
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                write_chunk = chunk[:remaining]
                if write_chunk:
                    log.write(write_chunk.decode("utf-8", errors="replace"))
                    log.flush()
                    remaining -= len(write_chunk)
                if len(write_chunk) != len(chunk):
                    truncated = True
            if truncated:
                log.write("\nCI_LOG_TRUNCATED: remainder discarded after 2.5 MB; worker drained output\n")
                log.flush()
        finally:
            proc.stdout.close()

    thread = threading.Thread(target=drain, name="ci-log-drain", daemon=True)
    thread.start()
    try:
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise RuntimeError("COMMAND_TIMEOUT") from exc
    finally:
        thread.join(timeout=10)
    if rc:
        raise RuntimeError("COMMAND_FAILED_EXIT_" + str(rc))


def execute_sha(sha: str, log) -> None:
    if not SHA_PATTERN.fullmatch(sha):
        raise ValueError("INVALID_SHA")
    if not CLI.is_file() or not os.access(CLI, os.X_OK):
        raise RuntimeError("WOODPECKER_CLI_MISSING")
    pipeline = APP / "trusted-main.yaml"
    if not pipeline.is_file():
        raise RuntimeError("TRUSTED_PIPELINE_MISSING")
    env = child_environment()
    python = VENV / "bin" / "python"
    if not PYTHON311.is_file() or not os.access(PYTHON311, os.X_OK):
        raise RuntimeError("PYTHON311_NOT_PROVISIONED")
    if not python.is_file():
        raise RuntimeError("PYTHON311_VENV_NOT_READY")
    python_version = run_simple([str(python), "--version"], timeout=10)
    if not python_version.startswith("Python 3.11."):
        raise RuntimeError("INCOMPATIBLE_PYTHON_VERSION")
    with tempfile.TemporaryDirectory(prefix="checkout-", dir=str(ROOT)) as work, \
         tempfile.TemporaryDirectory(prefix="jobtmp-", dir=str(ROOT / "tmp")) as run_tmp:
        env["TMPDIR"] = run_tmp
        checkout = Path(work) / "repo"
        logged_command(["git", "clone", "--quiet", "--no-checkout", "--depth", "1",
                        "--single-branch", "--branch", "main", REMOTE, str(checkout)],
                       log, cwd=Path(work), env=env, timeout=90)
        actual_sha = run_simple(["git", "rev-parse", "HEAD"], cwd=checkout, timeout=15)
        if actual_sha != sha:
            raise RuntimeError("REMOTE_ADVANCED_BEFORE_CHECKOUT")
        logged_command(["git", "checkout", "--quiet", "--detach", sha], log,
                       cwd=checkout, env=env, timeout=20)
        logged_command([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "-e", ".[test]"], log, cwd=checkout, env=env, timeout=600)
        # The trusted pipeline is stored OUTSIDE the cloned repository;
        # a GitHub push cannot change pipeline commands without review.
        logged_command([str(CLI), "lint", "--strict", str(pipeline)], log,
                       cwd=checkout, env=env, timeout=20)
        logged_command([str(CLI), "exec", "--backend-engine", "local",
                        "--repo-path", str(checkout), "--repo", REPO,
                        "--pipeline-event", "push", "--commit-branch", "main",
                        "--commit-sha", sha, str(pipeline)], log, cwd=checkout,
                       env=env, timeout=JOB_TIMEOUT_SECONDS)


@contextmanager
def locked():
    private_directory(ROOT)
    lockfile = ROOT / "run.lock"
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != '--retry-now'):
        print('usage: woodpecker_main_poll.py [--retry-now]')
        return 2
    force_retry = len(sys.argv) == 2
    private_directory(ROOT)
    with locked() as acquired:
        if not acquired:
            print("CI_LOCKED: another run owns the worker")
            return 0
        state_path = ROOT / "status.json"
        previous = read_json(state_path)
        try:
            sha = remote_sha()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            # Network failure never overwrites a previous PASS/FAIL as success.
            print("REMOTE_CHECK_ERROR:" + type(exc).__name__)
            return 3
        begin = time.time()
        if previous.get("sha") == sha and previous.get("profile") != PROFILE_VERSION:
            # A new verified runtime/test policy deserves one fresh bounded attempt.
            # Preserve evidence of the old run before resetting its retry counter.
            private_directory(ROOT / "history")
            write_json(ROOT / "history" / (sha[:12] + "-legacy-status.json"), previous)
            previous = {}
        go, reason, attempts = should_run(sha, previous, begin, force_retry=force_retry)
        if not go:
            print("CI_SKIP:", reason, sha[:12])
            return 0
        private_directory(ROOT / "logs")
        # New runtime profiles/restarts must not reuse old attempt filenames.
        # Preserve immutable logs from all previous CI profiles and retries.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_name = sha[:12] + "-" + PROFILE_VERSION + "-attempt" + str(attempts)
        log_path = ROOT / "logs" / (log_name + "-" + stamp + "-" + str(os.getpid()) + ".log")
        record = {
            "repository": REPO,
            "branch": "main",
            "sha": sha,
            "status": "RUNNING",
            "attempts": attempts,
            "trigger": reason,
            "started_at": utc(),
            "last_attempt_epoch": begin,
            "log_path": str(log_path),
            "engine": "woodpecker-cli-local",
            "profile": PROFILE_VERSION,
            "test_policy": "python3.11-fast-smoke-harness-fail-fast",
            "gate_level": "FAST",
            "full_regression": "NOT_RUN_NO_ISOLATED_DOCKER_RUNNER",
            "github_pr_checks": "NOT_CONNECTED",
            "log_limit_bytes": MAX_LOG_BYTES,
            "trust_scope": "protected-main-only",
        }
        write_json(state_path, record)
        print("CI_STARTED:", sha[:12], "attempt", attempts, flush=True)
        try:
            fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as log:
                log.write("ProjectFlow trusted-main CI " + sha + " started " + utc() + "\n")
                execute_sha(sha, log)
            record.update(status="FAST_PASS", reason="FAST_GATES_PASSED_FULL_NOT_RUN")
        except Exception as exc:
            # Don't echo arbitrary error strings (they may include shell output).
            record.update(status="FAIL", reason=type(exc).__name__ + ":" +
                          (str(exc) if isinstance(exc, RuntimeError) else "CHECK_FAILED"))
        record.update(finished_at=utc(), elapsed_seconds=round(time.time()-begin, 1))
        write_json(state_path, record)
        print("CI_FINISHED:", record["status"], sha[:12], record["reason"], flush=True)
        return 0 if record["status"] in ("PASS", "FAST_PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
