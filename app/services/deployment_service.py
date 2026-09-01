from __future__ import annotations
import re
import subprocess
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from app.services.project_contract import deployment_config, load_command
from app.services import ssrf_guard

"""Post-Merge DEV Deployment (section 2 of the spec this implements):
deliberately NOT a second deployment framework. Every real action here
is one of the SAME PROJECT.yaml-declared commands ProjectFlow already
runs for preflight/test/build (project_contract.load_command) -- this
service only orchestrates WHICH commands run in WHICH order, against
WHICH exact source commit, and persists the result. No route/template
ever constructs a shell command, hostname, or path itself (section 8/21).

Target audit (recorded here since it drove every design decision):
this host's REAL 'DEV LOCAL' Deploy Agent (127.0.0.1:8090) and its
target (mesflow-app/mesflow-postgres, port 8080) are the SAME running
containers nginx publicly serves as mesflow.net on 80/443 -- confirmed
via mesflow/reports/PROJECTFLOW_STANDARDIZATION_ASSESSMENT.md section 5
and the real /api/health response's own "server_role":"DEV" label,
which does NOT mean "safe to automate against" on this host. A prior,
independent audit already reached the same conclusion and built
`compose.projectflow-local.yml` (compose project mesflow-projectflow-
local, port 18280, its own bind-mount directory, no `build:` block)
specifically so ProjectFlow can safely drive BUILD -> DEPLOY -> HEALTH
-> SMOKE without ever touching the real target. THAT isolated sandbox,
reachable only through this repo's own commands.local_deploy/build/
smoke/local_status, is what "DEV" means everywhere in this feature --
never port 8090, never port 8080, never mesflow.net."""


class DeploymentError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_runner(argv: list[str], cwd, timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    """`env`, when given, is layered ON TOP of this process's own
    environment (never a replacement) -- used only for rollback's
    MESFLOW_IMAGE=<exact prior image> override (section 5), so the
    command still gets everything else (PATH, DOCKER_HOST, ...) it
    would normally see."""
    run_env = None
    if env:
        import os
        run_env = {**os.environ, **env}
    return subprocess.run(argv, cwd=str(cwd), shell=isinstance(argv, str), text=True, capture_output=True, timeout=timeout, env=run_env)


def _default_image_exists(image_ref: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image_ref], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


# Never let a command's own output leak a credential into a persisted
# phase log or the UI -- section 17/21. Deliberately broad (line-level,
# case-insensitive) rather than trying to enumerate every possible
# secret name; a false-positive redaction costs nothing, a leaked one
# costs everything.
_SECRET_LINE = re.compile(r"(password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*\S+", re.IGNORECASE)


def sanitize(text: str) -> str:
    if not text:
        return text
    return "\n".join(_SECRET_LINE.sub(lambda m: m.group(0).split(next(c for c in ":=" if c in m.group(0)))[0] + "=***REDACTED***", ln) for ln in text.splitlines())


class DeploymentService:
    """`runner` (argv/cwd/timeout -> CompletedProcess) and `spawn`
    (fn, args) are both injectable -- same DI pattern as GitHubMergeService/
    SandboxManager. Real subprocess + real background thread in
    production; tests substitute fakes that never actually shell out or
    run one, so every existing assertion right after calling deploy()
    still sees the already-finished result."""

    def __init__(self, db, git, runner=_default_runner, enforce_ssrf_guard: bool = False):
        self.db = db
        self.git = git
        self.runner = runner
        self.spawn = lambda fn, args=(): threading.Thread(target=fn, args=args, daemon=True).start()
        self.http_get = urllib.request.urlopen
        self.health_attempts = 20
        self.health_delay = 3.0
        self.image_exists = _default_image_exists
        # B1.2 (docs/B1_HOSTED_SERVICE_READ_ISOLATION.md, app/services/
        # ssrf_guard.py): only ever True when the caller (app/main.py)
        # already knows AUTH_MODE=='required' -- under AUTH_MODE=none
        # (the permanent self-hosted default) this stays False and
        # _check_health's real target-audit precedent (DEV == 127.0.0.1)
        # is byte-for-byte unchanged. Default False also preserves every
        # existing direct construction (every test in this repo).
        self.enforce_ssrf_guard = enforce_ssrf_guard
        # E10.19/E10.34: the env var a rollback pins to force the exact
        # prior artifact -- "MESFLOW_IMAGE" is this host's own real,
        # already-audited target's convention (see this module's own
        # target-audit docstring). Injectable so a non-Docker-artifact
        # target (e.g. this phase's own disposable HTTP fixture, which
        # has no docker image at all) can use its own env var name
        # without touching the real target's proven-safe default.
        self.rollback_env_var = "MESFLOW_IMAGE"

    # ---- target resolution (section 8) -----------------------------------
    def target(self, repo_path: str, environment: str) -> dict | None:
        """The trusted target/environment registry: PROJECT.yaml's own
        deployment.<environment> block. None means 'not configured' --
        never guessed, never a browser-supplied host/path substitute."""
        cfg = deployment_config(Path(repo_path), environment)
        if not cfg or not cfg.get("enabled"):
            return None
        return cfg

    # ---- environment-scoped commands (E10.12/E10.13) ----------------------
    def _command_for_env(self, repo_path: str, environment: str, name: str):
        """E10: an environment's own `deployment.<env>.commands.<name>`
        override (same {command, working_directory, timeout_seconds}
        shape load_command already uses), falling back to the EXACT
        existing global `commands.<name>` when no environment-specific
        override is declared. DEV has never declared a `deployment.DEV`
        block in any real PROJECT.yaml this app manages, so `target()`
        returns None and this always falls through to load_command() --
        zero behavior change for existing DEV automation. This is what
        lets TEST and PRODUCTION point at genuinely different targets
        (different port, different compose project, ...) without a
        second deploy mechanism."""
        cfg = self.target(repo_path, environment)
        if cfg and isinstance(cfg.get("commands"), dict):
            spec = cfg["commands"].get(name)
            if isinstance(spec, dict) and spec.get("command"):
                return (str(spec["command"]), str(spec.get("working_directory", ".")), int(spec.get("timeout_seconds", 1800)))
        return load_command(Path(repo_path), name)

    def _health_url_for_env(self, repo_path: str, environment: str) -> str | None:
        cfg = self.target(repo_path, environment)
        if cfg and isinstance(cfg.get("healthcheck"), dict) and cfg["healthcheck"].get("url"):
            import os
            def _sub(m):
                return os.environ.get(m.group(1), m.group(2))
            return re.sub(r"\$\{(\w+):-([^}]*)\}", _sub, cfg["healthcheck"]["url"])
        return self.health_url(repo_path)

    def health_url(self, repo_path: str) -> str | None:
        """service.healthcheck.url from PROJECT.yaml, with its own
        ${VAR:-default} shell-style placeholder resolved from the
        process environment the same way the project's own scripts
        would -- never a URL built from anything the browser supplied."""
        import os
        import yaml
        path = Path(repo_path) / "PROJECT.yaml"
        if not path.is_file():
            return None
        data = yaml.safe_load(path.read_text()) or {}
        url = ((data.get("service") or {}).get("healthcheck") or {}).get("url")
        if not url:
            return None
        def _sub(m):
            name, default = m.group(1), m.group(2)
            return os.environ.get(name, default)
        return re.sub(r"\$\{(\w+):-([^}]*)\}", _sub, url)

    # ---- phase bookkeeping ------------------------------------------------
    def _phase(self, deployment_id: int, phase: str):
        return self.db.execute(
            "INSERT INTO deployment_phases(deployment_id,phase,status,started_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
            (deployment_id, phase, "RUNNING"))

    def _phase_done(self, phase_id: int, status: str, result: subprocess.CompletedProcess | None = None):
        stdout = sanitize((result.stdout if result else "") or "")[-8000:]
        stderr = sanitize((result.stderr if result else "") or "")[-8000:]
        self.db.execute(
            "UPDATE deployment_phases SET status=?,stdout_tail=?,stderr_tail=?,exit_code=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, stdout, stderr, result.returncode if result else None, phase_id))

    def _set(self, deployment_id: int, **fields) -> None:
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE deployments SET {cols} WHERE id=?", (*fields.values(), deployment_id))

    def reconcile_on_startup(self) -> None:
        """P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a REAL,
        reproduced bug, same shape as AgentSessionManager.reconcile_on_
        startup() and CleanupWorker.reconcile()'s own sandbox fix --
        create_deployment()/redeploy()/rollback() (app/main.py) all
        refuse to act whenever `latest_deployment(...)['status']` is
        still PENDING/PREPARING/BUILDING/DEPLOYING/VERIFYING, so a
        deployment left in one of those by a process that restarted
        mid-operation (the real background `spawn()` thread doing the
        actual work dies with the OLD process) became permanently
        unrecoverable: every one of those three routes just redirects
        back to the same stuck row, forever, for that task/repo/
        environment. A server restart honestly lost that in-process
        work (identical reasoning to AgentSessionManager's own docstring
        on this) -- mark it FAILED with a clear, real reason so a fresh
        deployment/redeploy/rollback attempt is possible again."""
        for stuck in self.db.all(
                "SELECT id, status FROM deployments WHERE status IN ('PENDING','PREPARING','BUILDING','DEPLOYING','VERIFYING')"):
            self._set(stuck["id"], status="FAILED",
                       error=f"Deployment was {stuck['status']} when the server restarted; the in-process work was lost. Retry.",
                       finished_at=datetime.now(timezone.utc).isoformat())
            self.db.event("deployment", stuck["id"], "RECONCILE_STALE_OPERATION_STATE",
                           f"was {stuck['status']}, left running after a prior process restart -- marked FAILED, retriable again")

    # ---- the real orchestration --------------------------------------------
    def deploy(self, deployment_id: int) -> None:
        """Dispatches the real BUILD (if needed) -> DEPLOY -> HEALTH ->
        SMOKE chain to a background thread -- callers get an immediate
        PREPARING row back and poll for progress (button-state-ux),
        never blocking the request on real docker/build work."""
        self.spawn(self._run, (deployment_id,))

    def build_once(self, deployment_id: int, repo_path: str, source_commit: str, environment: str = "DEV") -> dict | None:
        """E10.9/E10.10: the ONE place build-once + artifact-identity
        logic lives -- extracted so ReleaseService.build() can reuse it
        for a build-only phase (via its own throwaway `deployments`
        row, environment='BUILD', purely to reuse this exact phase-
        audit mechanism) without duplicating a second build pipeline.
        Returns the artifact metadata dict (or None if the project
        declares no artifact metadata file at all) and always leaves
        the deployment row's own artifact_* columns updated when one
        exists. Raises DeploymentError on a genuine source mismatch --
        never deploys/records an artifact whose evidence contradicts
        the commit it claims to be built from."""
        build_cmd = self._command_for_env(repo_path, environment, "build")
        needs_build = self._needs_build(repo_path, source_commit)
        if build_cmd and needs_build:
            self._set(deployment_id, status="BUILDING")
            self._run_command(deployment_id, "BUILDING", build_cmd, repo_path)
        artifact = self._read_artifact_metadata(repo_path)
        if artifact:
            # section 4: an artifact metadata file exists (either just
            # built, or reused because it already matched -- see
            # _needs_build) but its OWN recorded source_commit must
            # still equal what THIS deployment was asked to deploy.
            # A mismatch here means either a build silently produced
            # evidence for the wrong commit, or a concurrent build for
            # a different commit clobbered the shared "latest"
            # metadata pointer after this one's own build finished --
            # never deploy an artifact whose evidence contradicts the
            # commit we were told to deploy.
            artifact_commit = artifact.get("source_commit")
            if artifact_commit and artifact_commit != source_commit:
                raise DeploymentError(
                    "ARTIFACT_SOURCE_MISMATCH",
                    f"artifact metadata source_commit {artifact_commit[:12]} != requested {source_commit[:12]}")
            self._set(deployment_id,
                      artifact_version=artifact.get("version"),
                      artifact_image=artifact.get("image"),
                      artifact_digest=artifact.get("image_digest"),
                      artifact_filename=artifact.get("package_filename"),
                      artifact_sha256=artifact.get("package_sha256"))
        return artifact

    def _run(self, deployment_id: int) -> None:
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (d["repository_id"],))
        repo_path = repo["repo_path"]
        try:
            self._set(deployment_id, status="PREPARING", started_at=now())
            self._prepare_source(deployment_id, repo_path, d["source_commit"])
            self.build_once(deployment_id, repo_path, d["source_commit"], d["environment"])

            deploy_cmd = self._command_for_env(repo_path, d["environment"], "local_deploy")
            if not deploy_cmd:
                raise DeploymentError("NO_DEPLOY_COMMAND", "PROJECT.yaml declares no local_deploy command")
            self._set(deployment_id, status="DEPLOYING")
            self._run_command(deployment_id, "DEPLOYING", deploy_cmd, repo_path)
            self._verify_health_and_smoke(deployment_id, repo_path, d["environment"])
            url = self._read_deployed_url(repo_path, d["environment"])
            self._set(deployment_id, status="VERIFIED", deployed_url=url, finished_at=now(), error=None)
        except DeploymentError as exc:
            self._set(deployment_id, status="FAILED", error=f"{exc.code}: {exc}"[:2000], finished_at=now())
        except Exception as exc:
            self._set(deployment_id, status="FAILED", error=str(exc)[:2000], finished_at=now())
        finally:
            self._restore_source(repo_path)

    def _verify_health_and_smoke(self, deployment_id: int, repo_path: str, environment: str = "DEV") -> None:
        """Shared by a normal deploy and a rollback -- both must pass the
        exact same real health+smoke bar before being called VERIFIED /
        ROLLED_BACK (section 8: 'not successful merely because docker
        compose starts')."""
        self._set(deployment_id, status="VERIFYING")
        health_url = self._health_url_for_env(repo_path, environment)
        healthy = self._check_health(deployment_id, health_url)
        self._set(deployment_id, health_status="PASS" if healthy else "FAIL", health_checked_at=now())
        if not healthy:
            raise DeploymentError("HEALTH_FAILED", f"Health check did not pass: {health_url}")

        smoke_cmd = self._command_for_env(repo_path, environment, "smoke")
        smoke_ok = True
        if smoke_cmd:
            smoke_ok = self._run_command(deployment_id, "SMOKE", smoke_cmd, repo_path, raise_on_fail=False)
        self._set(deployment_id, smoke_status="PASS" if smoke_ok else "FAIL")
        if not smoke_ok:
            raise DeploymentError("SMOKE_FAILED", "Smoke verification failed")

    def _read_deployed_url(self, repo_path: str, environment: str = "DEV") -> str | None:
        status_cmd = self._command_for_env(repo_path, environment, "local_status")
        if not status_cmd:
            return None
        r = self.runner(["bash", "-lc", status_cmd[0]], Path(repo_path) / status_cmd[1], status_cmd[2])
        m = re.search(r"^URL=(\S+)$", r.stdout or "", re.MULTILINE)
        return m.group(1) if m else None

    # ---- source pinning (section 3/24) -------------------------------------
    def _prepare_source(self, deployment_id: int, repo_path: str, source_commit: str) -> None:
        """Pins the SHARED repository checkout to the exact merge commit
        for the duration of the build -- a real, detached `git checkout`,
        never a guess, never the integration/agent branch. Verified as a
        real ancestor of origin/main first (never an arbitrary/unrelated
        commit); restored back onto `main` in _restore_source() whether
        this succeeds or fails, so every OTHER ProjectFlow operation that
        reads this checkout's `main` branch ref (worktree creation always
        reads the ref, never the working tree) is unaffected either way."""
        phase_id = self._phase(deployment_id, "PREPARING")
        try:
            self.git.git(repo_path, "fetch", "origin", "main")
            if not self._is_ancestor_of_origin_main(repo_path, source_commit):
                raise DeploymentError("SOURCE_NOT_ON_MAIN", f"{source_commit[:12]} is not an ancestor of origin/main -- refusing to deploy")
            r = self.git.git(repo_path, "checkout", "--detach", source_commit, check=False)
            if r.returncode:
                raise DeploymentError("CHECKOUT_FAILED", r.stderr.strip() or "git checkout failed")
            self._phase_done(phase_id, "SUCCESS", r)
        except DeploymentError:
            self._phase_done(phase_id, "FAILED")
            raise

    def _is_ancestor_of_origin_main(self, repo_path: str, commit: str) -> bool:
        return self.git.git(repo_path, "merge-base", "--is-ancestor", commit, "origin/main", check=False).returncode == 0

    def _restore_source(self, repo_path: str) -> None:
        try:
            self.git.git(repo_path, "checkout", "main", check=False)
            self.git.git(repo_path, "merge", "--ff-only", "origin/main", check=False)
        except Exception:
            pass  # best-effort restore -- never let this mask the real deploy result

    # ---- build-once / artifact identity (section 10) -----------------------
    def _read_artifact_metadata(self, repo_path: str) -> dict | None:
        import json
        try: data = self._read_repo_yaml(repo_path)
        except Exception: return None
        meta_rel = (data.get("artifacts") or {}).get("metadata")
        if not meta_rel: return None
        meta_path = (Path(repo_path) / meta_rel).resolve()
        if not meta_path.is_file(): return None
        try: return json.loads(meta_path.read_text())
        except Exception: return None

    def _needs_build(self, repo_path: str, source_commit: str) -> bool:
        """Build Once: if the last-built artifact's own recorded commit
        already IS this exact source_commit, reuse it -- never rebuild a
        different artifact between verification and deploy without a
        new identity (section 10)."""
        meta = self._read_artifact_metadata(repo_path)
        return not (meta and meta.get("source_commit") == source_commit)

    def _read_repo_yaml(self, repo_path: str) -> dict:
        import yaml
        return yaml.safe_load((Path(repo_path) / "PROJECT.yaml").read_text()) or {}

    # ---- command execution --------------------------------------------------
    def _run_command(self, deployment_id: int, phase: str, cmd: tuple, repo_path: str, raise_on_fail: bool = True, env: dict | None = None) -> bool:
        command, working_directory, timeout = cmd
        phase_id = self._phase(deployment_id, phase)
        cwd = (Path(repo_path) / working_directory).resolve()
        try:
            r = self.runner(["bash", "-lc", command], cwd, timeout, env=env) if env else self.runner(["bash", "-lc", command], cwd, timeout)
        except subprocess.TimeoutExpired as exc:
            self._phase_done(phase_id, "TIMEOUT")
            if raise_on_fail: raise DeploymentError(f"{phase}_TIMEOUT", f"{phase} timed out after {timeout}s") from exc
            return False
        ok = r.returncode == 0
        self._phase_done(phase_id, "SUCCESS" if ok else "FAILED", r)
        if not ok and raise_on_fail:
            raise DeploymentError(f"{phase}_FAILED", sanitize(r.stderr or r.stdout or f"{phase} failed")[:500])
        return ok

    def _check_health(self, deployment_id: int, url: str | None) -> bool:
        phase_id = self._phase(deployment_id, "VERIFYING")
        if not url:
            self._phase_done(phase_id, "FAILED")
            return False
        if self.enforce_ssrf_guard:
            try:
                ssrf_guard.check_url(url)
            except ssrf_guard.SSRFGuardError:
                # Checked once, up front -- not worth spending the full
                # health_attempts*health_delay budget retrying a target
                # this process will never be allowed to reach.
                self._phase_done(phase_id, "FAILED")
                return False
        for attempt in range(self.health_attempts):
            try:
                with self.http_get(url, timeout=5) as resp:
                    if 200 <= resp.status < 300:
                        self._phase_done(phase_id, "SUCCESS")
                        return True
            except Exception:
                pass
            if attempt < self.health_attempts - 1:
                time.sleep(self.health_delay)
        self._phase_done(phase_id, "FAILED")
        return False

    # ---- rollback (section 5/6/7/8) ----------------------------------------
    # Audit finding this whole capability is built on: mesflow's own
    # scripts/projectflow/deploy-local.sh already supports an explicit
    # MESFLOW_IMAGE=<image ref> environment override, checked BEFORE it
    # falls back to reading the "latest" artifact metadata or VERSION.txt
    # -- and it refuses to run at all ("image ... not found locally") if
    # that exact image isn't already present. That means a real rollback
    # is provably safe with zero new deploy machinery: run the exact same
    # local_deploy command, with MESFLOW_IMAGE pinned to a previous
    # VERIFIED deployment's own recorded artifact_image, and let that
    # existing script's own "must already exist" guard be the proof this
    # never silently rebuilds or deploys the wrong thing. No `build`
    # phase is ever run for a rollback.

    def rollback_target(self, deployment: dict) -> dict | None:
        """The previous VERIFIED deployment `deployment` could roll back
        to, or None if rollback genuinely isn't available right now --
        section 7: never show a fake/disabled rollback affordance. Real
        requirements: a strictly earlier VERIFIED deployment exists for
        the exact same repo+environment, it recorded an artifact image,
        and that image still exists on this host's docker.

        E10: a Release-driven TEST/PRODUCTION deployment has no single
        owning task_id (a Release may span multiple Tasks) -- those
        rows are created with task_id NULL, and SQL's NULL=NULL is
        never true, so the original task_id-scoped query would always
        find nothing for them. Matches by task_id when the deployment
        HAS one (the exact original per-Task DEV lineage, unchanged
        behavior); matches by repo+environment alone (task_id IS NULL)
        when it doesn't."""
        if deployment.get("task_id"):
            target = self.db.one(
                "SELECT * FROM deployments WHERE task_id=? AND repository_id=? AND environment=? AND status='VERIFIED' AND id<? ORDER BY id DESC LIMIT 1",
                (deployment["task_id"], deployment["repository_id"], deployment["environment"], deployment["id"]))
        else:
            target = self.db.one(
                "SELECT * FROM deployments WHERE task_id IS NULL AND repository_id=? AND environment=? AND status='VERIFIED' AND id<? ORDER BY id DESC LIMIT 1",
                (deployment["repository_id"], deployment["environment"], deployment["id"]))
        if not target or not target.get("artifact_image"):
            return None
        if not self.image_exists(target["artifact_image"]):
            return None
        return target

    def rollback(self, deployment_id: int) -> tuple[bool, str, int | None]:
        """Creates a new, append-only Deployment row recording the
        rollback attempt (never mutates the historical VERIFIED row --
        section 6) and dispatches it. Returns (ok, error, new_id)."""
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        if not d:
            return False, "Deployment not found", None
        target = self.rollback_target(d)
        if not target:
            return False, "No previous VERIFIED deployment available to roll back to", None
        new_id = self.db.execute(
            "INSERT INTO deployments(task_id,repository_id,environment,target_name,source_branch,source_commit,"
            "artifact_version,artifact_image,artifact_digest,artifact_filename,artifact_sha256,status,rollback_of,rollback_to_deployment_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?)",
            (d["task_id"], d["repository_id"], d["environment"], d["target_name"], target["source_branch"], target["source_commit"],
             target["artifact_version"], target["artifact_image"], target["artifact_digest"], target["artifact_filename"], target["artifact_sha256"],
             deployment_id, target["id"]))
        self.spawn(self._run_rollback, (new_id,))
        return True, "", new_id

    def _run_rollback(self, deployment_id: int) -> None:
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (d["repository_id"],))
        repo_path = repo["repo_path"]
        started = now()
        self._set(deployment_id, status="PREPARING", started_at=started, rollback_started_at=started)
        try:
            deploy_cmd = self._command_for_env(repo_path, d["environment"], "local_deploy")
            if not deploy_cmd:
                raise DeploymentError("NO_DEPLOY_COMMAND", "PROJECT.yaml declares no local_deploy command")
            self._set(deployment_id, status="DEPLOYING")
            # The one line that makes this a rollback rather than a
            # normal redeploy: force the exact previous artifact image,
            # never whatever "latest" happens to resolve to right now.
            self._run_command(deployment_id, "DEPLOYING", deploy_cmd, repo_path, env={self.rollback_env_var: d["artifact_image"]})
            self._verify_health_and_smoke(deployment_id, repo_path, d["environment"])
            url = self._read_deployed_url(repo_path, d["environment"])
            finished = now()
            self._set(deployment_id, status="ROLLED_BACK", rollback_status="VERIFIED", deployed_url=url,
                      finished_at=finished, rollback_finished_at=finished, error=None)
        except DeploymentError as exc:
            finished = now()
            msg = f"{exc.code}: {exc}"[:2000]
            self._set(deployment_id, status="ROLLBACK_FAILED", rollback_status="FAILED", rollback_error=msg, error=msg,
                      finished_at=finished, rollback_finished_at=finished)
        except Exception as exc:
            finished = now()
            msg = str(exc)[:2000]
            self._set(deployment_id, status="ROLLBACK_FAILED", rollback_status="FAILED", rollback_error=msg, error=msg,
                      finished_at=finished, rollback_finished_at=finished)
        # No _restore_source here: rollback never touches git / the shared
        # checkout at all -- it only ever redeploys an already-built,
        # already-tagged image that exists independently of any worktree.
