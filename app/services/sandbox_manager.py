from __future__ import annotations
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app.services.git_workspace import slugify
from app.services.port_allocator import PortAllocationError, PortAllocatorService
from app.services.sandbox_contract import (
    SandboxContractError,
    health_spec,
    hardware_build_command,
    load_sandbox_contract,
    output_specs,
    port_specs,
    profile_expose_lan,
    profile_services,
    resolve_profile,
)
from app.services.sandbox_runtime import SandboxRuntimeError, SandboxRuntimeService


def now() -> str: return datetime.now(timezone.utc).isoformat()


class SandboxError(RuntimeError):
    def __init__(self, code: str, message: str): self.code = code; super().__init__(message)


@dataclass
class SourceSpec:
    repository_id: int
    role: str
    branch: str
    commit_sha: str
    worktree_path: str
    repo_path: str
    source_type: str = "AGENT_WORKSPACE"


class SandboxManager:
    def __init__(self, db, runtime: SandboxRuntimeService, ports: PortAllocatorService, state_dir: Path, max_running: int = 3, default_retention_hours: int = 24):
        self.db = db
        self.runtime = runtime
        self.ports = ports
        self.state_dir = Path(state_dir)
        self.max_running = max_running
        self.default_retention_hours = default_retention_hours
        # Injectable, real-by-default (matches the runner/launcher DI
        # pattern already used elsewhere in this codebase): provision/
        # reset_data/cleanup's slow docker work runs via self.spawn so a
        # route can redirect immediately instead of blocking the whole
        # request on `docker compose up/down` (button-state-ux). Tests
        # override this to run synchronously inline (see conftest.py) so
        # every existing assertion right after calling one of these
        # methods keeps seeing the already-finished real result --
        # docker itself is still 100% real either way, only the thread
        # scheduling differs.
        self.spawn = lambda fn, args=(): threading.Thread(target=fn, args=args, daemon=True).start()

    # ---- capacity -------------------------------------------------
    def running_count(self) -> int:
        """State-consistency audit finding: CLEANUP_ELIGIBLE is a
        retention-countdown label, not a runtime state -- that sandbox's
        real container is still up and consuming a capacity slot for
        the whole grace window (cleanup() is what actually tears it
        down, never mark_cleanup_eligible() alone). Excluding it here
        previously let more real containers run simultaneously than
        max_running actually allows once any Task started completing."""
        return self.db.one("SELECT COUNT(*) n FROM sandboxes WHERE status IN ('RUNNING','STARTING','PROVISIONING','CLEANUP_ELIGIBLE')")["n"]

    def capacity_available(self) -> bool:
        return self.running_count() < self.max_running

    # ---- creation ---------------------------------------------------
    def create(self, *, task_id: int | None, owner_type: str, owner_id: int, profile: str, provider: SourceSpec, extra_sources: list[SourceSpec] | None = None) -> int | None:
        """Create (but do not yet provision) a sandbox row. Returns None if
        profile resolves to NONE -- per spec, no sandbox row exists at all
        for a NONE-profile owner."""
        if profile == "NONE":
            return None
        contract = load_sandbox_contract(Path(provider.repo_path))
        if contract is None:
            raise SandboxError("SANDBOX_CONTRACT_REQUIRED", f"{Path(provider.repo_path).name} declares no sandbox: contract in PROJECT.yaml")
        if profile not in (contract.get("profiles") or {}) and profile.lower() not in (contract.get("profiles") or {}):
            raise SandboxError("SANDBOX_PROFILE_UNKNOWN", f"Profile not declared by project: {profile}")

        suffix = uuid.uuid4().hex[:6]
        repo_slug = slugify(Path(provider.repo_path).name)
        owner_slug = f"{owner_type.lower()}-{owner_id}"
        sandbox_slug = f"{repo_slug}-{owner_slug}-{suffix}"
        compose_project = f"wm-{repo_slug}-{owner_slug}-{suffix}"[:63]

        env_dir = self.state_dir / "sandboxes" / sandbox_slug
        env_dir.mkdir(parents=True, exist_ok=True)

        sandbox_id = self.db.execute(
            "INSERT INTO sandboxes(task_id,repository_id,owner_type,owner_id,sandbox_slug,profile,runtime_type,compose_project,status,worktree_path,environment_path) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, provider.repository_id, owner_type, owner_id, sandbox_slug, profile, "docker-compose", compose_project, "CREATED", provider.worktree_path, str(env_dir)),
        )

        all_sources = [provider, *(extra_sources or [])]
        for src in all_sources:
            self.db.execute(
                "INSERT INTO sandbox_sources(sandbox_id,repository_id,role,branch,commit_sha,worktree_path,source_type) VALUES(?,?,?,?,?,?,?)",
                (sandbox_id, src.repository_id, src.role, src.branch, src.commit_sha, src.worktree_path, src.source_type),
            )
        self._write_manifest(sandbox_id)
        self.db.event("sandbox", sandbox_id, "SANDBOX_CREATED", sandbox_slug)
        return sandbox_id

    # ---- manifest -----------------------------------------------------
    def _write_manifest(self, sandbox_id: int) -> dict:
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        sources = self.db.all("SELECT s.*,r.repo_name FROM sandbox_sources s JOIN repositories r ON r.id=s.repository_id WHERE s.sandbox_id=?", (sandbox_id,))
        manifest = {
            "sandbox": sb["sandbox_slug"],
            "profile": sb["profile"],
            "sources": {src["role"]: {"repo": src["repo_name"], "branch": src["branch"], "commit": src["commit_sha"]} for src in sources},
            "runtime": {"profile": sb["profile"]},
        }
        ports = self.ports.ports_for(sandbox_id)
        if ports:
            manifest["runtime"]["ports"] = {p["service"]: p["host_port"] for p in ports}
        manifest_path = Path(sb["environment_path"]) / "source-manifest.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        self.db.execute("UPDATE sandboxes SET source_manifest_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(manifest), sandbox_id))
        return manifest

    # ---- staleness ------------------------------------------------------
    def is_stale(self, sandbox_id: int, current_commits: dict[int, str]) -> bool:
        """current_commits: {repository_id: current HEAD of that source's branch}.
        Caller resolves current HEADs via git (this module has no git
        dependency by design -- keeps it testable without a git fixture)."""
        sources = self.db.all("SELECT repository_id,commit_sha FROM sandbox_sources WHERE sandbox_id=?", (sandbox_id,))
        return any(current_commits.get(s["repository_id"]) not in (None, s["commit_sha"]) for s in sources)

    # ---- provisioning ---------------------------------------------------
    def provision(self, sandbox_id: int) -> None:
        """Validates + flips the sandbox to PROVISIONING synchronously
        (so a route that just called this can redirect immediately and
        the very next page load, even from a different tab, already sees
        PROVISIONING -- never the stale pre-click status), then runs the
        actual `docker compose up` + health check in a background thread.
        Button-feedback: this is what makes 'Provisioning...' survive a
        refresh instead of freezing the tab for the whole docker call."""
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        if not sb: raise SandboxError("SANDBOX_NOT_FOUND", "sandbox not found")
        if not self.capacity_available() and sb["status"] not in ("RUNNING", "STARTING", "PROVISIONING", "CLEANUP_ELIGIBLE"):
            self.db.execute(
                "UPDATE sandboxes SET status='FAILED',error_code=?,error_message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                ("SANDBOX_CAPACITY_FULL", f"Max {self.max_running} running sandboxes reached", sandbox_id),
            )
            self.db.event("sandbox", sandbox_id, "SANDBOX_PROVISION_FAILED", "SANDBOX_CAPACITY_FULL")
            raise SandboxError("SANDBOX_CAPACITY_FULL", f"Max {self.max_running} running sandboxes reached")

        op_id = self._op_start(sandbox_id, "PROVISION")
        self.db.execute("UPDATE sandboxes SET status='PROVISIONING',updated_at=CURRENT_TIMESTAMP WHERE id=?", (sandbox_id,))
        self.spawn(self._provision_worker, (sandbox_id, op_id))

    def _provision_worker(self, sandbox_id: int, op_id: int) -> None:
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        try:
            provider = self.db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1", (sandbox_id,))
            repo_path = Path(provider["worktree_path"])
            contract = load_sandbox_contract(repo_path)
            if contract is None: raise SandboxError("SANDBOX_CONTRACT_REQUIRED", "sandbox: contract missing")
            services = profile_services(contract, sb["profile"])
            specs = port_specs(contract)

            env_lines = [f"COMPOSE_PROJECT_NAME={sb['compose_project']}"]
            outputs: dict[str, str] = {}
            for service in services:
                if service not in specs: continue
                spec = specs[service]
                port = self.ports.allocate(sandbox_id, service, spec["container"], spec["range"])
                env_lines.append(f"WM_PORT_{service.upper()}={port}")
                outputs[service] = f"http://127.0.0.1:{port}"

            if sb["profile"] == "HARDWARE":
                lan_ip = self.runtime.local_ip()
                if lan_ip:
                    for service, url in list(outputs.items()):
                        env_lines.append(f"WM_LAN_{service.upper()}={lan_ip}")

            env_lines.append(f"WM_SANDBOX_ID={sandbox_id}")
            env_path = Path(sb["environment_path"]) / ".env"
            env_path.write_text("\n".join(env_lines) + "\n")

            self._op_update(op_id, "RUNNING")
            self.db.execute("UPDATE sandboxes SET status='STARTING',updated_at=CURRENT_TIMESTAMP,started_at=CURRENT_TIMESTAMP WHERE id=?", (sandbox_id,))
            compose_file = repo_path / contract["compose_file"]
            result = self.runtime.compose_up(sb["compose_project"], compose_file, env_path, repo_path, services, sandbox_id)
            self._op_finish(op_id, "SUCCESS", 0, result.stdout, result.stderr)

            self._write_manifest(sandbox_id)
            self._record_outputs(sandbox_id, contract, outputs)
            self.health_check(sandbox_id)
        except SandboxRuntimeError as exc:
            self._op_finish(op_id, "FAILED", 1, exc.stdout, exc.stderr)
            self.db.execute("UPDATE sandboxes SET status='FAILED',error_code=?,error_message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (exc.code, str(exc), sandbox_id))
            self.db.event("sandbox", sandbox_id, "SANDBOX_PROVISION_FAILED", exc.code)
            # source worktree is never touched/deleted on a provisioning failure
        except SandboxError as exc:
            self._op_finish(op_id, "FAILED", 1, "", str(exc))
            self.db.execute("UPDATE sandboxes SET status='FAILED',error_code=?,error_message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (exc.code, str(exc), sandbox_id))
            self.db.event("sandbox", sandbox_id, "SANDBOX_PROVISION_FAILED", exc.code)
            # running in a background thread now -- already fully recorded
            # above; nothing left to catch a re-raise here, so don't.

    def _record_outputs(self, sandbox_id: int, contract: dict, port_outputs: dict[str, str]) -> None:
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        manifest = json.loads(sb["source_manifest_json"])
        # "outputs" (named, contract-declared, e.g. backend_url) is kept
        # separate from "runtime" (internal bookkeeping: profile/ports) so
        # a consumer enumerating outputs never has to filter out
        # non-output bookkeeping keys.
        named = manifest.setdefault("outputs", {})
        for name, spec in output_specs(contract).items():
            service = spec.get("service")
            if service in port_outputs:
                named[name] = port_outputs[service]
        self.db.execute("UPDATE sandboxes SET source_manifest_json=? WHERE id=?", (json.dumps(manifest), sandbox_id))
        manifest_path = Path(sb["environment_path"]) / "source-manifest.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    def outputs(self, sandbox_id: int) -> dict:
        """Named, contract-declared outputs (e.g. backend_url) -- what a
        consumer (frontend/ESP build, cross-repo test) should read."""
        sb = self.db.one("SELECT source_manifest_json FROM sandboxes WHERE id=?", (sandbox_id,))
        return json.loads(sb["source_manifest_json"]).get("outputs", {}) if sb else {}

    def service_urls(self, sandbox_id: int) -> dict:
        """{service_name: http://127.0.0.1:<port>} -- keyed by the raw
        compose service name (matches PROJECT.yaml's sandbox.health keys),
        distinct from outputs() which is keyed by the contract's own
        output name."""
        sb = self.db.one("SELECT source_manifest_json FROM sandboxes WHERE id=?", (sandbox_id,))
        if not sb: return {}
        ports_by_service = json.loads(sb["source_manifest_json"]).get("runtime", {}).get("ports", {})
        return {service: f"http://127.0.0.1:{port}" for service, port in ports_by_service.items()}

    # ---- health -----------------------------------------------------
    def health_check(self, sandbox_id: int, attempts: int = 5, delay_seconds: float = 0.5) -> bool:
        """A container just Started by `compose up -d` is not necessarily
        already accepting connections yet -- retry briefly (bounded, ~2.5s
        total by default) before concluding UNHEALTHY, matching docs
        section 18's PROVISIONING -> STARTING -> health check -> RUNNING
        flow (a container that is merely "Up" is not sufficient on its own)."""
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        provider = self.db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1", (sandbox_id,))
        contract = load_sandbox_contract(Path(provider["worktree_path"]))
        urls = self.service_urls(sandbox_id)
        health_specs = (contract.get("health") or {}).items()
        healthy = True
        for service, spec in health_specs:
            url = urls.get(service)
            if not url:
                continue
            ok = False
            for attempt in range(attempts):
                ok, _ = self.runtime.health_check(url.rstrip("/") + spec.get("path", "/"))
                if ok or attempt == attempts - 1: break
                time.sleep(delay_seconds)
            healthy = healthy and ok
        status = "RUNNING" if healthy else "UNHEALTHY"
        self.db.execute(
            "UPDATE sandboxes SET status=?,health_status=?,last_health_check=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, "HEALTHY" if healthy else "UNHEALTHY", sandbox_id),
        )
        self.db.event("sandbox", sandbox_id, "HEALTH_CHECK", status)
        return healthy

    # ---- lifecycle -----------------------------------------------------
    def stop(self, sandbox_id: int) -> None:
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        provider = self.db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1", (sandbox_id,))
        contract = load_sandbox_contract(Path(provider["worktree_path"]))
        env_path = Path(sb["environment_path"]) / ".env"
        compose_file = Path(provider["worktree_path"]) / contract["compose_file"]
        op_id = self._op_start(sandbox_id, "STOP")
        result = self.runtime.compose_stop(sb["compose_project"], compose_file, env_path, Path(provider["worktree_path"]))
        self._op_finish(op_id, "SUCCESS" if result.returncode == 0 else "FAILED", result.returncode, result.stdout, result.stderr)
        self.db.execute("UPDATE sandboxes SET status='STOPPED',stopped_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (sandbox_id,))
        self.db.event("sandbox", sandbox_id, "SANDBOX_STOPPED")

    def reset_data(self, sandbox_id: int) -> None:
        """RESET_DATA (section 7): recreate this sandbox's own mutable
        data from scratch -- for testing default-credential/first-run/
        seed/migration behavior, never a raw `docker compose down -v`
        exposed to the browser. Verifies ownership (the same
        compose_project label check cleanup() already uses) before
        removing anything, stops+removes only the volumes docker-compose
        associates with THIS exact compose project, then re-provisions
        from the current exact source commit. Sandbox identity
        (compose_project/sandbox_slug) and allocated ports are both
        preserved: ports.allocate() is idempotent per (sandbox_id,
        service) and is never released here, so provision() below hands
        back the SAME host ports rather than allocating new ones."""
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        if not sb: raise SandboxError("SANDBOX_NOT_FOUND", "sandbox not found")
        if not self.runtime.verify_owned(sb["compose_project"], sandbox_id):
            raise SandboxError("OWNERSHIP_UNVERIFIED", "refusing to reset unlabeled resources")
        provider = self.db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1", (sandbox_id,))
        if not provider: raise SandboxError("NO_SOURCE", "sandbox has no source to rebuild from")
        contract = load_sandbox_contract(Path(provider["worktree_path"]))
        if contract is None: raise SandboxContractError("SANDBOX_CONTRACT_REQUIRED", "sandbox: contract missing")
        # Everything above is a fast, local, synchronous validation --
        # everything below is the slow docker-backed part, so the status
        # flip to RESETTING happens here, before returning to the route,
        # exactly like provision()'s PROVISIONING flip (button-feedback:
        # 'Resetting data...' is correct even on an immediate refresh).
        op_id = self._op_start(sandbox_id, "RESET_DATA")
        self.db.execute("UPDATE sandboxes SET status='RESETTING',updated_at=CURRENT_TIMESTAMP WHERE id=?", (sandbox_id,))
        self.db.event("sandbox", sandbox_id, "SANDBOX_RESET_REQUESTED", provider["commit_sha"])
        self.spawn(self._reset_data_worker, (sandbox_id, sb, provider, contract, op_id))

    def _reset_data_worker(self, sandbox_id: int, sb: dict, provider: dict, contract: dict, op_id: int) -> None:
        env_path = Path(sb["environment_path"]) / ".env"
        compose_file = Path(provider["worktree_path"]) / contract["compose_file"]
        try:
            result = self.runtime.compose_down(sb["compose_project"], compose_file, env_path, Path(provider["worktree_path"]), remove_volumes=True)
            self._op_finish(op_id, "SUCCESS" if result.returncode == 0 else "FAILED", result.returncode, result.stdout, result.stderr)
            if result.returncode != 0:
                self.db.execute("UPDATE sandboxes SET status='FAILED',error_code='RESET_DATA_FAILED',error_message=? WHERE id=?", (result.stderr[-2000:], sandbox_id))
                return
        except SandboxRuntimeError as exc:
            self._op_finish(op_id, "FAILED", 1, exc.stdout, exc.stderr)
            self.db.execute("UPDATE sandboxes SET status='FAILED',error_code=?,error_message=? WHERE id=?", (exc.code, str(exc), sandbox_id))
            return
        # Data volumes are gone; source/branch/commit/ports are untouched.
        # provision() re-runs `compose up` (which recreates the removed
        # volumes fresh) against the exact same source, then the usual
        # health check -- the same tracked path a normal Start goes
        # through. provision() does its own quick sync flip + spawns its
        # own worker thread -- calling it from inside this worker thread
        # is fine, it never blocks the original request either way.
        self.provision(sandbox_id)

    def mark_cleanup_eligible(self, sandbox_id: int, retention_hours: int | None = None) -> None:
        hours = self.default_retention_hours if retention_hours is None else retention_hours
        eligible_at = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        self.db.execute("UPDATE sandboxes SET status='CLEANUP_ELIGIBLE',cleanup_eligible_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('CLOSED','CLEANING')", (eligible_at, sandbox_id))
        self.db.event("sandbox", sandbox_id, "CLEANUP_SCHEDULED", eligible_at)

    def cleanup(self, sandbox_id: int, force: bool = False) -> None:
        sb = self.db.one("SELECT * FROM sandboxes WHERE id=?", (sandbox_id,))
        if not sb: return
        if sb["status"] in ("CLOSED", "CLEANING"): return
        if not force and sb["status"] not in ("CLEANUP_ELIGIBLE", "STOPPED", "FAILED"):
            raise SandboxError("SANDBOX_NOT_CLEANUP_ELIGIBLE", f"status={sb['status']}")
        if not self.runtime.verify_owned(sb["compose_project"], sandbox_id):
            raise SandboxError("OWNERSHIP_UNVERIFIED", "refusing to clean unlabeled resources")

        # CLEANING was already a recognized transitional status elsewhere
        # (cleanup_worker's stuck-state reconciliation, mark_cleanup_
        # eligible's guard) but nothing ever actually set it -- a Cleanup
        # click looked unchanged on the button/status badge for the
        # entire compose-down. Set it synchronously here, before
        # dispatching the real teardown to a background thread.
        op_id = self._op_start(sandbox_id, "CLEANUP")
        self.db.execute("UPDATE sandboxes SET status='CLEANING',updated_at=CURRENT_TIMESTAMP WHERE id=?", (sandbox_id,))
        provider = self.db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? ORDER BY id LIMIT 1", (sandbox_id,))
        self.spawn(self._cleanup_worker, (sandbox_id, sb, provider, op_id))

    def _cleanup_worker(self, sandbox_id: int, sb: dict, provider: dict | None, op_id: int) -> None:
        try:
            contract = load_sandbox_contract(Path(provider["worktree_path"])) if provider else None
            if contract:
                env_path = Path(sb["environment_path"]) / ".env"
                compose_file = Path(provider["worktree_path"]) / contract["compose_file"]
                result = self.runtime.compose_down(sb["compose_project"], compose_file, env_path, Path(provider["worktree_path"]))
                self._op_finish(op_id, "SUCCESS" if result.returncode == 0 else "FAILED", result.returncode, result.stdout, result.stderr)
            else:
                self._op_finish(op_id, "SUCCESS", 0, "", "no provider contract; nothing to tear down")
        except SandboxRuntimeError as exc:
            self._op_finish(op_id, "FAILED", 1, exc.stdout, exc.stderr)
        self.ports.release(sandbox_id)
        # generated env file is removed; the source manifest / operation
        # history / audit events in the DB are never deleted here.
        env_dir = Path(sb["environment_path"]) if sb["environment_path"] else None
        if env_dir and env_dir.exists():
            env_file = env_dir / ".env"
            if env_file.exists(): env_file.unlink()
        self.db.execute("UPDATE sandboxes SET status='CLOSED',cleaned_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (sandbox_id,))
        self.db.event("sandbox", sandbox_id, "SANDBOX_CLEANED")

    # ---- operations bookkeeping -----------------------------------------
    def _op_start(self, sandbox_id: int, op_type: str) -> int:
        return self.db.execute("INSERT INTO sandbox_operations(sandbox_id,operation_type,status) VALUES(?,?,?)", (sandbox_id, op_type, "RUNNING"))
    def _op_update(self, op_id: int, status: str) -> None:
        self.db.execute("UPDATE sandbox_operations SET status=? WHERE id=?", (status, op_id))
    def _op_finish(self, op_id: int, status: str, exit_code: int, stdout: str, stderr: str) -> None:
        self.db.execute(
            "UPDATE sandbox_operations SET status=?,finished_at=CURRENT_TIMESTAMP,exit_code=?,stdout_tail=?,stderr_tail=? WHERE id=?",
            (status, exit_code, (stdout or "")[-20000:], (stderr or "")[-20000:], op_id),
        )
