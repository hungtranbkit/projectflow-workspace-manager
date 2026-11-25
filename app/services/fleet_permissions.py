"""Owner-scoped fleet bootstrap v1. Audit is read-only and the CLI default.

No credential output, root agents, policy edits or production restarts. The
profile is an explicit trust receipt for ONE canonical worktree and branch.
Shell, Git credentials, MCP grants and OS privilege remain separate gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.services.git_workspace import GitWorkspaceService, GitSafetyError, noninteractive_git_env
from app.services.sandbox_runtime import fleet_container_argv as container_argv, fleet_docker_probe, run_fleet_canary

VERSION = 1
PROFILE = "trusted-project-unattended-v1"


class FleetError(RuntimeError):
    pass


def classify(returncode, output):
    if returncode == 0:
        return "PASS"
    text = output.lower()
    for code, needles in (
        ("STATUS_PROBE_TIMEOUT", ("status_probe_timeout",)),
        ("SUDO_REQUIRED", ("sudo:", "no new privileges")),
        ("AUTH_REQUIRED", ("could not read username", "terminal prompts disabled", "authentication failed", "publickey", "not logged", "http 401", "login required", "gh auth login")),
        ("GIT_WRITE_DENIED", ("http 403", "write access", "protected branch", "permission to", "resource not accessible")),
        ("TRUST_REQUIRED", ("trust this", "not trusted", "trust dialog")),
        ("CLI_UNSUPPORTED", ("unexpected argument", "unknown option", "unrecognized", "not found")),
        ("DISK_LOW", ("no space left",)),
        ("ACL_DENIED", ("permission denied", "access is denied")),
        ("OFFLINE", ("node_unreachable", "no route to host", "network is unreachable", "connection refused")),
    ):
        if any(needle in text for needle in needles):
            return code
    return "PROBE_FAILED"


@dataclass
class Probe:
    code: str
    returncode: int | None
    elapsed_ms: int
    output: str = ""  # private, never serialized in reports

    def public(self):
        return {"code": self.code, "returncode": self.returncode, "elapsed_ms": self.elapsed_ms}


def probe(argv, *, cwd=None, timeout=12, env=None):
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=noninteractive_git_env(env),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, shell=False,
                                start_new_session=os.name != "nt")
        try:
            output, _ = proc.communicate(timeout=timeout)
            code = classify(proc.returncode, output)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                # taskkill only this newly spawned probe and its descendants.
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=5)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate(timeout=5)
            code = "PROBE_TIMEOUT"
        return Probe(code, proc.returncode, int((time.monotonic()-started)*1000), output)
    except FileNotFoundError:
        return Probe("CLI_UNSUPPORTED", None, int((time.monotonic()-started)*1000))
    except PermissionError:
        return Probe("ACL_DENIED", None, int((time.monotonic()-started)*1000))


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise FleetError("ACL_DENIED: symlink state file")
    # Exclusive temporary file, atomic replacement, private from creation.
    temp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def agent_argv(agent, workspace, prompt):
    if agent == "codex":
        return ["codex", "exec", "--sandbox", "workspace-write", "-c", 'approval_policy="never"',
                "-c", "check_for_update_on_startup=false", "-c", "sandbox_workspace_write.network_access=true",
                "--cd", str(workspace), "--", prompt]
    if agent == "claude":
        # No broad Bash permission: normal scoped Git operations go through
        # the owner-reviewed wrapper; other commands fail closed in dontAsk.
        return ["claude", "--print", "--permission-mode", "dontAsk",
                "--allowedTools", "Read,Edit,Write,Glob,Grep",
                "--disallowedTools", "Bash(sudo *),Bash(git push --force*),Bash(git reset --hard*),Bash(git clean *)",
                "--", prompt]
    raise FleetError("CLI_UNSUPPORTED")


def execution_result(result):
    code = result.code
    if "bwrap: No permissions to create a new namespace" in result.output:
        code = "ACL_DENIED"
    elif "failed to spawn code-mode host" in result.output:
        code = "CLI_UNSUPPORTED"
    return {**result.public(), "code": code, "execution": "EXITED" if code == "PASS" else "BLOCKED",
            "dispatched": True, "task_outcome": "UNVERIFIED"}




class FleetProfile:
    def __init__(self, workspace, state=None, repo="hungtranbkit/projectflow-workspace-manager"):
        self.workspace = Path(workspace).expanduser().resolve()
        self.state = Path(state or Path.home()/".local/state/projectflow/fleet-v1").expanduser().resolve()
        self.path = self.state / (digest(str(self.workspace).encode())[:20] + ".json")
        self.backup = self.path.with_suffix(".backup.json")
        self.git = GitWorkspaceService(self.workspace.parent, timeout=20)
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise FleetError("INVALID_REPOSITORY")
        self.repo = repo

    def identity(self, clean=False):
        if platform.system() == "Windows":
            env = dict(os.environ, PF83_WORKSPACE=str(self.workspace), PF83_STATE=str(self.state))
            script = r"""$ErrorActionPreference='Stop'; $id=[Security.Principal.WindowsIdentity]::GetCurrent();
$p=[Security.Principal.WindowsPrincipal]::new($id);
if($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 1};
$owner=(Get-Acl -LiteralPath $env:PF83_WORKSPACE).GetOwner([Security.Principal.SecurityIdentifier]);
if($owner -ne $id.User){exit 1};
if(Test-Path -LiteralPath $env:PF83_STATE){
foreach($r in (Get-Acl -LiteralPath $env:PF83_STATE).Access){
if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]) -ne $id.User){exit 1}}}; exit 0"""
            guard = probe(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], env=env)
            if guard.code != "PASS":
                raise FleetError("ACL_DENIED: Windows owner/elevation/private-state check failed")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise FleetError("ROOT_AGENT_DENIED")
        if not self.workspace.is_dir() or not os.access(self.workspace, os.W_OK):
            raise FleetError("ACL_DENIED")
        if hasattr(os, "getuid") and self.workspace.stat().st_uid != os.getuid():
            raise FleetError("ACL_DENIED: workspace owner")
        self.git.validate_repo(self.workspace)
        branch = self.git.git(self.workspace, "branch", "--show-current").stdout.strip()
        if not re.fullmatch(r"agent/[a-z0-9-]+/[a-z0-9-]+", branch):
            raise FleetError("TRUST_REQUIRED: dedicated agent branch required")
        if not (self.workspace/".git").is_file():
            raise FleetError("TRUST_REQUIRED: isolated linked worktree required")
        if clean and self.git.status(self.workspace).strip():
            raise FleetError("DIRTY_WORKSPACE")
        configs = {}
        for dirname in (".codex", ".claude"):
            base = self.workspace/dirname
            if base.exists():
                if not base.resolve().is_relative_to(self.workspace):
                    raise FleetError("TRUST_REQUIRED: external agent configuration directory")
                for path in sorted(base.rglob("*")):
                    if not path.resolve().is_relative_to(self.workspace):
                        raise FleetError("TRUST_REQUIRED: external symlink agent configuration")
                    if path.is_file():
                        configs[str(path.relative_to(self.workspace))] = digest(path.read_bytes())
        return {"workspace": str(self.workspace), "branch": branch, "config_hashes": configs}

    def apply(self, trust_owned=False):
        if not trust_owned:
            raise FleetError("TRUST_REQUIRED: explicit --trust-owned-workspace required")
        identity = self.identity(clean=True)
        manifest = {"schema_version": VERSION, "profile": PROFILE, "repo": self.repo, **identity}
        text = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        if self.path.exists():
            if self.path.read_text() == text:
                return {"code": "PASS", "changed": False, "profile": PROFILE}
            raise FleetError("PROFILE_CONFLICT: rollback before changing trust")
        private_write(self.backup, json.dumps({"schema_version": VERSION, "before": None, "after_sha256": digest(text.encode())}))
        private_write(self.path, text)
        return {"code": "PASS", "changed": True, "profile": PROFILE}

    def check(self):
        if not self.path.exists():
            raise FleetError("TRUST_REQUIRED: no owner trust receipt")
        if self.path.is_symlink() or (os.name != "nt" and self.path.stat().st_mode & 0o077):
            raise FleetError("ACL_DENIED: trust receipt is not private")
        manifest = json.loads(self.path.read_text())
        current = self.identity()
        if manifest != {"schema_version": VERSION, "profile": PROFILE, "repo": self.repo, **current}:
            raise FleetError("TRUST_REQUIRED: workspace/branch/config changed")
        return {"code": "PASS", "profile": PROFILE}

    def rollback(self):
        if not self.path.exists():
            return {"code": "PASS", "changed": False}
        receipt = json.loads(self.backup.read_text())
        if digest(self.path.read_bytes()) != receipt["after_sha256"]:
            raise FleetError("ROLLBACK_CONFLICT: preserve later edits")
        self.path.unlink()
        return {"code": "PASS", "changed": True}

    def audit(self):
        checks = {}
        commands = {
            "sudo": ["sudo", "-n", "true"],
            "gh_auth": ["gh", "auth", "status", "--hostname", "github.com"],
            "gh_identity": ["gh", "api", "user", "--jq", ".login"],
            "github_push": ["gh", "api", f"repos/{self.repo}", "--jq", ".permissions.push"],
            "codex_version": ["codex", "--version"], "claude_version": ["claude", "--version"],
            "codex_auth": ["codex", "login", "status"], "claude_auth": ["claude", "auth", "status"],
        }
        with ThreadPoolExecutor(max_workers=4) as executor:
            pending = {name: executor.submit(probe, argv, cwd=self.workspace) for name, argv in commands.items()}
            results = {name: future.result() for name, future in pending.items()}
        results["docker"] = fleet_docker_probe(["info", "--format", "{{.ServerVersion}}"], cwd=self.workspace)
        for name, result in results.items():
            if name == "claude_auth":
                try:
                    if json.loads(result.output).get("loggedIn") is False:
                        result.code = "AUTH_REQUIRED"
                except (ValueError, AttributeError):
                    pass
            if name == "github_push" and result.code == "PASS" and result.output.strip() != "true":
                result.code = "AUTH_REQUIRED" if results["gh_identity"].code != "PASS" else "GIT_WRITE_DENIED"
            checks[name] = result.public()
            if name.endswith("_version") and result.code == "PASS":
                version = re.search(r"\d+\.\d+\.\d+", result.output)
                checks[name]["version"] = version.group() if version else "UNKNOWN"
        origin = self.git.git(self.workspace, "remote", "get-url", "origin", check=False).stdout.strip()
        accepted = (f"https://github.com/{self.repo}", f"git@github.com:{self.repo}", f"ssh://git@github.com/{self.repo}")
        matches = origin.removesuffix(".git") in accepted
        push_urls = self.git.git(self.workspace, "remote", "get-url", "--push", "--all", "origin", check=False).stdout.splitlines()
        matches = matches and push_urls == [origin]
        checks["repository"] = {"code": "PASS" if matches else "TRUST_REQUIRED"}
        helper = self.git.git(self.workspace, "config", "--get-urlmatch", "credential.helper", "https://github.com", check=False)
        checks["git_helper"] = {"code": "CONFIGURED" if helper.returncode == 0 and helper.stdout.strip() else "AUTH_REQUIRED"}
        if matches and not origin.startswith("https://"):
            checks["git_helper"] = {"code": "CONFIGURED", "transport": "SSH_REQUIRES_WRITE_CANARY"}
        try:
            self.identity()
            checks["workspace"] = {"code": "PASS", "dirty": bool(self.git.status(self.workspace).strip())}
        except (FleetError, ValueError, RuntimeError) as exc:
            checks["workspace"] = {"code": safe_error_code(exc)}
        free = shutil.disk_usage(self.workspace).free
        inodes = os.statvfs(self.workspace).f_favail if hasattr(os, "statvfs") else None
        checks["disk"] = {"code": "DISK_LOW" if free < 512*1024*1024 or inodes == 0 else "PASS", "free_bytes": free, "free_inodes": inodes}
        checks["serial_adb"] = {"code": "NOT_REQUIRED", "reason": "Git/CLI task; no device access requested"}
        checks["admin"] = {"code": "PASS" if checks["sudo"]["code"] == "PASS" else "OWNER_BOOTSTRAP_REQUIRED", "required_for_task": False}
        policy_files = [Path("/etc/codex/requirements.toml"), Path("/etc/codex/managed_config.toml"),
                        Path("/etc/claude-code/managed-settings.json"),
                        Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
        if os.name == "nt":
            policy_files.append(Path(os.environ.get("ProgramFiles", "C:/Program Files"))/"ClaudeCode/managed-settings.json")
        checks["managed_policy"] = {"code": "OBSERVED", "present": [str(p) for p in policy_files if p.exists()],
                                    "effective": "NOT_VERIFIED_BY_AUDIT"}
        return {"schema_version": VERSION, "profile": PROFILE, "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "os": platform.system(), "node": platform.node(), "uid": os.getuid() if hasattr(os, "getuid") else None,
                "executables": {name: shutil.which(name) for name in ("git", "gh", "codex", "claude", "docker", "sudo")},
                "checks": checks, "write_canary": "NOT_RUN", "execution": "NOT_DISPATCHED"}

    def preflight(self, agent, runtime="native", image=None):
        self.check()
        report = self.audit()
        required = ["workspace", "repository", "gh_auth", "gh_identity", "github_push", "git_helper", "disk", agent + "_auth", agent + "_version"]
        failures = {key: report["checks"][key]["code"] for key in required if report["checks"][key]["code"] not in ("PASS", "CONFIGURED")}
        help_result = probe([agent, "exec", "--help"] if agent == "codex" else [agent, "--help"])
        flags = ("--sandbox", "--config", "--cd") if agent == "codex" else ("--print", "dontAsk", "--allowedTools", "--disallowedTools")
        if help_result.code != "PASS" or not all(flag in help_result.output for flag in flags):
            failures["cli_options"] = "CLI_UNSUPPORTED"
        if runtime == "docker":
            if report["checks"].get("managed_policy", {}).get("present"):
                failures["managed_policy"] = "TRUST_REQUIRED"
            if not all(flag in help_result.output for flag in ("--ephemeral", "--skip-git-repo-check")):
                failures["cli_options"] = "CLI_UNSUPPORTED"
            if agent != "codex" or platform.system() != "Linux" or not image:
                failures["runtime"] = "CLI_UNSUPPORTED"
            elif report["checks"]["docker"]["code"] != "PASS":
                failures["runtime"] = report["checks"]["docker"]["code"]
            elif fleet_docker_probe(["image", "inspect", image]).code != "PASS":
                failures["runtime"] = "CLI_UNSUPPORTED"
        elif agent == "codex" and platform.system() == "Linux":
            sandbox = probe(["codex", "sandbox", "--", "/bin/true"], cwd=self.workspace)
            if sandbox.code != "PASS":
                failures["sandbox"] = "ACL_DENIED" if "namespace" in sandbox.output else sandbox.code
        return {"code": "BLOCKED" if failures else "PASS", "failures": failures, "execution": "NOT_DISPATCHED"}

    def launch(self, agent, prompt, timeout=180, runtime="native", image=None):
        gate = self.preflight(agent, runtime, image)
        if gate["code"] != "PASS":
            return gate
        # Exclusive lane lock. A timed-out probe kills only its own process group.
        lock = self.path.with_suffix(".lock")
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise FleetError("BLOCKED: lane already locked; inspect checkpoint before recovery")
        try:
            os.close(fd)
            checkpoint = self.path.with_suffix(".checkpoint.json")
            private_write(checkpoint, json.dumps({**self.identity(), "head": self.git.head(self.workspace), "state": "STARTING"}))
            env = noninteractive_git_env()
            env["DISABLE_AUTOUPDATER"] = "1"
            command = agent_argv(agent, self.workspace, prompt)
            container_name = None
            if runtime == "docker":
                container_name = f"wm-projectflow-fleet{os.getuid()}-" + uuid.uuid4().hex
                auth = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))/"auth.json"
                if not auth.is_file() or auth.stat().st_uid != os.getuid():
                    raise FleetError("AUTH_REQUIRED")
                command = container_argv(self.workspace, prompt, Path(shutil.which("codex")).resolve(),
                                         auth.resolve(), image, os.getuid(), os.getgid(), container_name)
            if container_name:
                result = run_fleet_canary(command, container_name, self.workspace, timeout, env)
            else:
                result = probe(command, cwd=self.workspace, timeout=timeout, env=env)
            receipt = {**execution_result(result), "runtime": runtime,
                       "capabilities": "FILE_WORK_ONLY_NO_GIT" if runtime == "docker" else "NATIVE_CLI_POLICY"}
            private_write(checkpoint, json.dumps({**self.identity(), "head": self.git.head(self.workspace), **receipt}))
            return receipt
        finally:
            lock.unlink(missing_ok=True)


def safe_error_code(exc):
    if isinstance(exc, FleetError):
        code = str(exc).split(":")[0]
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
            return code
    if isinstance(exc, GitSafetyError):
        return "TRUST_REQUIRED"
    if isinstance(exc, PermissionError):
        return "ACL_DENIED"
    return "PROBE_FAILED"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="audit", choices=("audit", "apply", "check", "rollback", "preflight", "launch", "git-canary", "install-gh", "rollback-gh"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path)
    parser.add_argument("--repo", default="hungtranbkit/projectflow-workspace-manager")
    parser.add_argument("--trust-owned-workspace", action="store_true")
    parser.add_argument("--agent", choices=("codex", "claude"), default="codex")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--runtime", choices=("native", "docker"), default="native")
    parser.add_argument("--image", help="Existing pinned Linux image ID; never pulls an image")
    args = parser.parse_args(argv)
    try:
        manager = FleetProfile(args.workspace, args.state, args.repo)
        if args.action in ("install-gh", "rollback-gh"):
            from app.services import fleet_gh
            result = fleet_gh.install() if args.action == "install-gh" else fleet_gh.rollback()
        elif args.action == "apply":
            result = manager.apply(args.trust_owned_workspace)
        elif args.action == "preflight":
            result = manager.preflight(args.agent, args.runtime, args.image)
        elif args.action == "git-canary":
            manager.check()
            if manager.audit()["checks"]["repository"]["code"] != "PASS":
                raise FleetError("TRUST_REQUIRED: origin does not match authorized repository")
            result = manager.git.push_canary(args.workspace)
        elif args.action == "launch":
            if not args.prompt_file:
                raise FleetError("PROMPT_REQUIRED")
            result = manager.launch(args.agent, args.prompt_file.read_text(), args.timeout, args.runtime, args.image)
        else:
            result = getattr(manager, args.action)()
    except (FleetError, OSError, ValueError, RuntimeError) as exc:
        result = {"code": safe_error_code(exc), "execution": "BLOCKED", "dispatched": False}
    print(json.dumps(result, indent=2))
    return 0 if result.get("code", "PASS") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
