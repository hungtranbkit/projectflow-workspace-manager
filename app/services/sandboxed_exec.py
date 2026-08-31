from __future__ import annotations
import subprocess
from pathlib import Path

from app.services.project_contract import load_exec_sandbox
from app.services.sandbox_runtime import SandboxRuntimeError, SandboxRuntimeService
from app.services.secret_redaction import redact

"""B0.6 -- mandatory sandboxing (docs/B0_HOSTED_PLATFORM_SECURITY_
FOUNDATION.md). The one shared chokepoint every PROJECT.yaml-declared
command execution (TestRunner's preflight/test stages, GateWaiverService's
baseline-probe re-run -- both real, audited `shell=True` call sites,
see the B0 audit table) now goes through, replacing each call site's
own direct `subprocess.run(..., shell=True)`.

Policy, not mechanism, is the only thing this class decides:
`AUTH_MODE=none` (self-hosted, permanent per ADR-004) keeps running
commands directly on the host -- today's exact, already-verified
behavior, zero new surface, matching every other B0.1-B0.5 guard's own
no-op precedent. `AUTH_MODE=required` makes every one of these
executions mandatory-sandboxed (SandboxRuntimeService.run_ephemeral) --
never a silent unsandboxed fallback (fail closed); a repo that hasn't
declared its own `exec_sandbox:` block still gets a real, safe default
profile (project_contract.load_exec_sandbox's own defaults), it is
never treated as "sandboxing not required for this repo."

Return-value/exception shape is normalized to match exactly what the
direct `subprocess.run(..., shell=True)` call these call sites used to
make would have produced, so neither call site needed to change its
own success/timeout handling -- only the one line making the call."""


class SandboxedCommandRunner:
    def __init__(self, sandbox_runtime: SandboxRuntimeService, mandatory: bool):
        self.sandbox_runtime = sandbox_runtime
        self.mandatory = mandatory

    def run(self, command: str, worktree_root: Path, working_dir: str, timeout: int,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        known_secrets = list((env or {}).values())

        def _redacted(proc: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
            # B0.7: every executed-command result -- test output, gate-
            # baseline re-runs, firmware builds -- is a real log/output
            # surface (test_runs.stdout_tail/stderr_tail is rendered
            # straight into the UI). Redact both the specific values this
            # call injected via `env` AND the fixed credential-shaped
            # patterns (secret_redaction.py), regardless of AUTH_MODE --
            # a strict improvement over today's exact behavior.
            proc.stdout = redact(proc.stdout, known_secrets) if proc.stdout else proc.stdout
            proc.stderr = redact(proc.stderr, known_secrets) if proc.stderr else proc.stderr
            return proc

        if not self.mandatory:
            import os
            full_env = {**os.environ, **env} if env else None
            return _redacted(subprocess.run(
                command, cwd=(worktree_root / (working_dir or ".")).resolve(),
                shell=True, text=True, capture_output=True, timeout=timeout, env=full_env))
        profile = load_exec_sandbox(worktree_root)
        try:
            return _redacted(self.sandbox_runtime.run_ephemeral(
                command, worktree_root, working_dir, timeout,
                profile["image"], profile["network"], profile["memory"], profile["cpus"], profile["pids_limit"],
                env=env))
        except SandboxRuntimeError as exc:
            if exc.code == "RUNTIME_TIMEOUT":
                # Re-raised as the exact exception type every existing
                # caller's `except subprocess.TimeoutExpired` already
                # handles -- the sandboxed path's timeout looks
                # identical to the host path's own timeout from the
                # caller's point of view.
                raise subprocess.TimeoutExpired(
                    cmd=command, timeout=timeout,
                    output=redact(exc.stdout, known_secrets), stderr=redact(exc.stderr, known_secrets)) from exc
            # A startup failure (docker unavailable, unknown image, a
            # malformed exec_sandbox.image typo, ...) is a real, clean
            # FAILURE result -- returncode 1, the reason in stderr --
            # never an unhandled exception crashing the background
            # test-run thread (adversarial "startup failure" coverage).
            return _redacted(subprocess.CompletedProcess(
                args=command, returncode=1, stdout=exc.stdout,
                stderr=f"SANDBOX_{exc.code}: {exc}\n{exc.stderr}"))
