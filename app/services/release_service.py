from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

"""Release, Build-Once Artifact, TEST/PRODUCTION Deploy & Rollback
(Phase E10.7-E10.26). Deliberately NOT a second deployment framework
-- every real build/deploy/health/rollback action delegates to the
EXISTING DeploymentService (deployments/deployment_phases, already
real: build-once artifact reuse, real health+smoke verification, real
rollback pinned to a prior artifact). ReleaseService adds exactly what
didn't exist: a durable Release lifecycle wrapping one or more
integrated Tasks around ONE immutable artifact identity, shared
unchanged between TEST and PRODUCTION, with binding production
approval and a migration-safety classification gate."""

RELEASE_STATUSES = (
    "DRAFT", "BUILDING", "BUILT", "QUALIFYING", "READY", "FAILED",
    "DEPLOYING_TEST", "TEST_VERIFIED",
    "WAITING_PRODUCTION_APPROVAL", "DEPLOYING_PRODUCTION", "PRODUCTION_VERIFIED",
    "ROLLED_BACK",
)
MIGRATION_CLASSIFICATIONS = ("NO_MIGRATION", "BACKWARD_COMPATIBLE", "ROLLBACK_SAFE", "DESTRUCTIVE", "UNKNOWN")
_DESTRUCTIVE_KEYWORDS = ("drop table", "drop column", "delete from", "truncate", "irreversible", "destroy", "cannot be undone", "data loss")
_SAFE_KEYWORDS = ("backward compatible", "additive", "reversible", "safe to roll back", "no data loss")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReleaseError(ValueError):
    pass


class ReleaseService:
    def __init__(self, db, changes, work_products, workflow_service, human_decisions, deployment_service,
                 project_policy_resolver=None):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.workflow_service = workflow_service
        self.human_decisions = human_decisions
        self.deployment_service = deployment_service
        self.project_policy_resolver = project_policy_resolver

    # ---- reads ------------------------------------------------------
    def get(self, release_id: int) -> dict | None:
        return self.db.one("SELECT * FROM releases WHERE id=?", (release_id,))

    def list_for_repository(self, repository_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM releases WHERE repository_id=? ORDER BY id DESC", (repository_id,))

    def tasks_for(self, release_id: int) -> list[dict]:
        return self.db.all(
            "SELECT rt.*, t.title FROM release_tasks rt JOIN tasks t ON t.id=rt.task_id WHERE rt.release_id=? ORDER BY rt.task_id", (release_id,))

    # ---- E10.8: versioning -- reuse the project's own convention -----
    def _next_version(self, repo_path: str, repository_id: int) -> str | None:
        """Deterministic version identity, when one exists: `VERSION`
        file content, else `PROJECT.yaml`'s `project.version`. None
        means no deterministic source exists -- the caller falls back
        to _auto_increment_version(), the only path with an actual
        read-then-write race (B2.1, docs/B2_RELEASE_CONCURRENCY_AND_
        RESIDUAL_SECURITY.md): a file-backed or caller-supplied version
        is the same string every time it's resolved, so a genuine
        collision on it is a real duplicate, never a race artifact --
        retrying it would just recompute the identical value forever."""
        vf = Path(repo_path) / "VERSION"
        if vf.is_file() and vf.read_text().strip():
            return vf.read_text().strip()
        try:
            import yaml
            data = yaml.safe_load((Path(repo_path) / "PROJECT.yaml").read_text()) or {}
            v = (data.get("project") or {}).get("version")
            if v:
                return str(v)
        except Exception:
            pass
        return None

    def _auto_increment_version(self, repository_id: int) -> str:
        n = self.db.one("SELECT COUNT(*) c FROM releases WHERE repository_id=?", (repository_id,))["c"]
        return f"v{n + 1}"

    # ---- E10.7/E10.25: create ------------------------------------------
    def create_release(self, repository_id: int, task_ids: list[int], version: str | None = None) -> dict:
        if not task_ids:
            raise ReleaseError("A Release needs at least one integrated Task")
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (repository_id,))
        if not repo:
            raise ReleaseError("Unknown repository_id")
        merges = []
        for tid in task_ids:
            m = self.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=? ORDER BY id DESC LIMIT 1", (tid, repository_id))
            if not m or m["merge_status"] != "MERGED":
                raise ReleaseError(f"Task {tid} is not INTEGRATED (MERGED) into this repository yet")
            merges.append(m)
        source_commit = merges[-1]["merged_commit"]

        # B2.1: a deterministic version (explicit param, VERSION file, or
        # PROJECT.yaml's project.version) resolves to the SAME string on
        # every attempt -- a collision on it is a real duplicate, so it
        # gets exactly one pre-check and one insert attempt, same as
        # before this fix (never a pointless 5x retry of an identical
        # value). Only the COUNT-based auto-increment fallback is
        # genuinely racy (two concurrent callers can read the same
        # COUNT before either commits) and gets the bounded-retry-on-
        # collision treatment -- the SAME pattern already proven for
        # plans.(change_id,revision) and execution_waves.wave_number
        # (both P0.9).
        deterministic_version = version or self._next_version(repo["repo_path"], repository_id)
        release_id = None
        resolved_version = None
        attempts = 1 if deterministic_version is not None else 5
        for _attempt in range(attempts):
            this_version = deterministic_version or self._auto_increment_version(repository_id)
            if self.db.one("SELECT id FROM releases WHERE repository_id=? AND version=?", (repository_id, this_version)):
                if deterministic_version is not None:
                    raise ReleaseError(f"Version {this_version!r} already exists for this repository -- a Release version must be unique per artifact digest")
                # Auto-increment path: a concurrent caller's insert
                # landed between our own read and this check -- exactly
                # the race this loop exists to absorb. Recompute fresh
                # and retry, never raise for a value nothing but our own
                # stale read ever "chose".
                continue
            try:
                release_id = self.db.execute(
                    "INSERT INTO releases(repository_id,version,source_commit,spec_baseline_work_product_id,design_baseline_work_product_id,"
                    "test_design_baseline_work_product_id,status,work_product_id) VALUES(?,?,?,?,?,?,?,?)",
                    (repository_id, this_version, source_commit, None, None, None, "DRAFT", None))
                resolved_version = this_version
                break
            except Exception as exc:
                if deterministic_version is not None:
                    # The race window on a deterministic value: another
                    # caller's insert landed between our pre-check and
                    # our own insert. Same clear error the pre-check
                    # itself gives -- never a generic retry-exhausted
                    # message for something that IS a real duplicate.
                    raise ReleaseError(
                        f"Version {this_version!r} already exists for this repository -- a Release version must be unique per artifact digest") from exc
                continue
        if release_id is None:
            raise ReleaseError("Could not allocate a Release version (concurrent creation contention) -- please retry")
        version = resolved_version

        t0 = self.db.one("SELECT change_id FROM tasks WHERE id=?", (task_ids[0],))
        change_id = t0["change_id"] if t0 else None
        baselines = self._baseline_wp_ids(change_id) if change_id else {}
        # Built AFTER the version race is fully resolved -- never inside
        # the retry loop above, so a collision-retry can never leave an
        # orphaned WorkProduct behind, and this title always reflects
        # the version that actually won (never one that lost the race).
        wp_id = self.work_products.create(
            kind="RELEASE_MANIFEST", title=f"Release {version} ({repo['repo_name']})", change_id=change_id, status="DRAFT",
            content_metadata={"repository_id": repository_id, "version": version, "source_commit": source_commit, "task_ids": task_ids})
        self.db.execute(
            "UPDATE releases SET spec_baseline_work_product_id=?,design_baseline_work_product_id=?,"
            "test_design_baseline_work_product_id=?,work_product_id=? WHERE id=?",
            (baselines.get("spec"), baselines.get("design"), baselines.get("test_design"), wp_id, release_id))
        for tid, m in zip(task_ids, merges):
            self.db.execute("INSERT INTO release_tasks(release_id,task_id,merged_commit) VALUES(?,?,?)", (release_id, tid, m["merged_commit"]))
        self.db.event("release", release_id, "RELEASE_CREATED", f"version={version} tasks={task_ids}")
        return self.get(release_id)

    def _baseline_wp_ids(self, change_id: int) -> dict:
        def latest(kind):
            rows = [wp for wp in self.work_products.list_for_change(change_id) if wp["kind"] == kind and wp["status"] == "APPROVED"]
            return rows[-1]["id"] if rows else None
        return {"spec": latest("FEATURE_SPEC"), "design": latest("TECHNICAL_DESIGN"), "test_design": latest("TEST_CASE_SET")}

    def _set(self, release_id: int, **fields) -> None:
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE releases SET {cols},updated_at=CURRENT_TIMESTAMP WHERE id=?", (*fields.values(), release_id))

    # ---- E10.9/E10.11: build once, verified ----------------------------
    def build(self, release_id: int) -> dict:
        r = self.get(release_id)
        if not r:
            raise ReleaseError("Release not found")
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (r["repository_id"],))
        self._set(release_id, status="BUILDING")
        # A throwaway `deployments` row, environment='BUILD', purely to
        # reuse DeploymentService's own phase-audit mechanism -- never a
        # second build pipeline, and 'BUILD' is never a real deploy
        # target (TEST/PRODUCTION deploys create their OWN rows below).
        deployment_id = self.db.execute(
            "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status) VALUES(?,?,?,?,?)",
            (r["repository_id"], "BUILD", repo["default_branch"], r["source_commit"], "PENDING"))
        try:
            self.deployment_service._prepare_source(deployment_id, repo["repo_path"], r["source_commit"])
            artifact = self.deployment_service.build_once(deployment_id, repo["repo_path"], r["source_commit"])
        except Exception as exc:
            self._set(release_id, status="FAILED", error=str(exc)[:2000])
            return {"outcome": "BUILD_FAILED", "release_id": release_id, "message": str(exc)}
        finally:
            self.deployment_service._restore_source(repo["repo_path"])

        d = self.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        if not artifact or not (d["artifact_digest"] or d["artifact_sha256"] or d["artifact_image"]):
            self._set(release_id, status="FAILED", error="ARTIFACT_INVALID: no artifact digest/image produced")
            return {"outcome": "ARTIFACT_INVALID", "release_id": release_id}
        if d["artifact_version"] and d["artifact_version"] != r["version"] and str(d["artifact_version"]) != str(r["version"]):
            # A project that stamps its own version into artifact
            # metadata must agree with the Release's own version --
            # never silently accept a mismatched artifact identity.
            self._set(release_id, status="FAILED", error=f"ARTIFACT_INVALID: artifact version {d['artifact_version']!r} != release version {r['version']!r}")
            return {"outcome": "ARTIFACT_INVALID", "release_id": release_id}

        wp_id = self.work_products.create(
            kind="BUILD_ARTIFACT", title=f"Build artifact {r['version']}", status="APPROVED",
            content_ref=d["artifact_digest"] or d["artifact_sha256"] or d["artifact_image"],
            content_metadata={"release_id": release_id, "version": r["version"], "source_commit": r["source_commit"],
                                "digest": d["artifact_digest"] or d["artifact_sha256"], "image": d["artifact_image"],
                                "filename": d["artifact_filename"], "built_at": now()})
        self._set(release_id, status="BUILT", artifact_version=d["artifact_version"] or r["version"],
                    artifact_image=d["artifact_image"], artifact_digest=d["artifact_digest"], artifact_filename=d["artifact_filename"],
                    artifact_sha256=d["artifact_sha256"], build_evidence=json.dumps({"deployment_id": deployment_id, "work_product_id": wp_id}))
        self.db.event("release", release_id, "RELEASE_BUILT", f"digest={d['artifact_digest'] or d['artifact_sha256']}")
        return {"outcome": "BUILT", "release_id": release_id, "artifact_digest": d["artifact_digest"] or d["artifact_sha256"]}

    # ---- E10.21: migration safety --------------------------------------
    def classify_migration(self, release_id: int) -> str:
        r = self.get(release_id)
        design_wp = self.work_products.get(r["design_baseline_work_product_id"]) if r["design_baseline_work_product_id"] else None
        if not design_wp:
            return "NO_MIGRATION"
        meta = json.loads(design_wp["content_metadata"] or "{}")
        plan = (meta.get("migration_plan") or "").strip()
        if not plan:
            return "NO_MIGRATION"
        low = plan.lower()
        if any(k in low for k in _DESTRUCTIVE_KEYWORDS):
            return "DESTRUCTIVE"
        compat = (meta.get("backward_compatibility") or "").strip().lower()
        if any(k in low or k in compat for k in _SAFE_KEYWORDS):
            return "ROLLBACK_SAFE" if "roll back" in low or "rollback" in low else "BACKWARD_COMPATIBLE"
        return "UNKNOWN"

    # ---- E10.22: qualification -- aggregate, never re-derive ----------
    def qualify(self, release_id: int, review_fix_orchestrator) -> dict:
        r = self.get(release_id)
        if not r:
            raise ReleaseError("Release not found")
        if r["status"] not in ("BUILT", "QUALIFYING", "READY", "WAITING_PRODUCTION_APPROVAL"):
            return {"outcome": "BLOCKED", "release_id": release_id, "reason": f"Release is {r['status']}, must be BUILT first"}
        self._set(release_id, status="QUALIFYING")
        blockers = []
        for rt in self.tasks_for(release_id):
            tid = rt["task_id"]
            if review_fix_orchestrator.review_pass(tid) is False:
                blockers.append(f"CODE_REVIEW_NOT_PASS:{tid}")
            if review_fix_orchestrator.security_pass(tid) is False:
                blockers.append(f"SECURITY_REVIEW_NOT_PASS:{tid}")
            t = self.db.one("SELECT change_id FROM tasks WHERE id=?", (tid,))
            if t and t["change_id"] and self.human_decisions.pending_for_change(t["change_id"]):
                blockers.append(f"WAITING_HUMAN:{tid}")
        migration = self.classify_migration(release_id)
        self._set(release_id, migration_classification=migration)
        if blockers:
            outcome = "WAITING_HUMAN" if any(b.startswith("WAITING_HUMAN") for b in blockers) else "BLOCKED"
            self._set(release_id, status="FAILED" if outcome == "BLOCKED" else "QUALIFYING", error="; ".join(blockers)[:2000])
            return {"outcome": outcome, "release_id": release_id, "blockers": blockers}
        self._set(release_id, status="READY", error=None)
        self.db.event("release", release_id, "RELEASE_READY", f"migration={migration}")
        return {"outcome": "RELEASE_READY", "release_id": release_id, "migration_classification": migration}

    # ---- E10.13: deploy TEST --------------------------------------------
    def deploy_test(self, release_id: int) -> dict:
        r = self.get(release_id)
        if not r or r["status"] not in ("READY", "TEST_VERIFIED"):
            return {"outcome": "BLOCKED", "release_id": release_id, "reason": "Release must be READY before a TEST deploy"}
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (r["repository_id"],))
        deployment_id = self.db.execute(
            "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,artifact_version,artifact_image,"
            "artifact_digest,artifact_filename,artifact_sha256,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r["repository_id"], "TEST", repo["default_branch"], r["source_commit"], r["artifact_version"], r["artifact_image"],
             r["artifact_digest"], r["artifact_filename"], r["artifact_sha256"], "PENDING"))
        self._set(release_id, status="DEPLOYING_TEST", test_deployment_id=deployment_id)
        self.deployment_service.deploy(deployment_id)
        return {"outcome": "DEPLOYING", "release_id": release_id, "deployment_id": deployment_id}

    def _runtime_verification_wp(self, release_id: int, deployment_id: int, kind_label: str) -> int:
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        passed = d["status"] == "VERIFIED" and d["health_status"] == "PASS"
        return self.work_products.create(
            kind="RUNTIME_VERIFICATION", title=f"{kind_label} runtime verification ({d['environment']})",
            status="APPROVED" if passed else "REJECTED",
            content_metadata={"release_id": release_id, "deployment_id": deployment_id, "environment": d["environment"],
                                "status": d["status"], "health_status": d["health_status"], "smoke_status": d["smoke_status"],
                                "artifact_digest": d["artifact_digest"] or d["artifact_sha256"], "deployed_url": d["deployed_url"]})

    def sync_test_result(self, release_id: int) -> dict:
        """Explicit-tick observer -- deploy() runs on a background
        thread (DeploymentService's own established pattern); this
        reads the now-current deployment row and advances the Release
        accordingly. Never assumes success from a bare call return."""
        r = self.get(release_id)
        if not r or not r["test_deployment_id"]:
            return {"outcome": "NO_TEST_DEPLOYMENT", "release_id": release_id}
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (r["test_deployment_id"],))
        if d["status"] in ("PENDING", "PREPARING", "BUILDING", "DEPLOYING", "VERIFYING"):
            return {"outcome": "DEPLOYING", "release_id": release_id, "deployment_status": d["status"]}
        wp_id = self._runtime_verification_wp(release_id, d["id"], "TEST")
        if d["status"] == "VERIFIED":
            self._set(release_id, status="TEST_VERIFIED")
            self.db.event("release", release_id, "RELEASE_TEST_VERIFIED", f"deployment={d['id']} work_product={wp_id}")
            return {"outcome": "RUNTIME_VERIFIED", "release_id": release_id, "work_product_id": wp_id}
        self._set(release_id, status="FAILED", error=d["error"])
        self.db.event("release", release_id, "RELEASE_TEST_FAILED", f"deployment={d['id']}")
        return {"outcome": "RUNTIME_VERIFY_FAILED", "release_id": release_id, "work_product_id": wp_id, "message": d["error"]}

    # ---- E10.17: production approval, bound to release+digest+target ---
    def require_production_approval(self, release_id: int) -> bool:
        r = self.get(release_id)
        controlled = False
        for rt in self.tasks_for(release_id):
            t = self.db.one("SELECT change_id FROM tasks WHERE id=?", (rt["task_id"],))
            if t and t["change_id"]:
                run = self.workflow_service.get_workflow(t["change_id"])
                if run and run["profile_key"] == "CONTROLLED":
                    controlled = True
        if controlled:
            return True
        if self.project_policy_resolver:
            try:
                repo = self.db.one("SELECT * FROM repositories WHERE id=?", (r["repository_id"],))
                policy = self.project_policy_resolver(repo) or {}
                return bool((policy.get("release") or {}).get("require_production_approval"))
            except Exception:
                return False
        return False

    def approve_production(self, release_id: int, approved_by: str) -> dict:
        r = self.get(release_id)
        if not r or r["status"] != "TEST_VERIFIED":
            return {"outcome": "BLOCKED", "release_id": release_id, "reason": "Release must be TEST_VERIFIED before production approval"}
        if r["migration_classification"] in ("DESTRUCTIVE", "UNKNOWN") and not approved_by:
            return {"outcome": "APPROVAL_REQUIRED", "release_id": release_id, "reason": f"migration classification {r['migration_classification']} requires explicit human approval"}
        self._set(release_id, status="WAITING_PRODUCTION_APPROVAL", production_approved_by=approved_by,
                    production_approved_at=now(), production_approval_digest=r["artifact_digest"] or r["artifact_sha256"])
        self.db.event("release", release_id, "PRODUCTION_APPROVED", f"by={approved_by} digest={r['artifact_digest']}")
        return {"outcome": "APPROVED", "release_id": release_id}

    def _production_approval_valid(self, r: dict) -> bool:
        if not r["production_approved_by"]:
            return False
        current_digest = r["artifact_digest"] or r["artifact_sha256"]
        # E10.36: approval binds to release id + artifact digest +
        # target -- a later rebuild/different digest invalidates it.
        return bool(current_digest) and r["production_approval_digest"] == current_digest

    # ---- E10.16/E10.18: promotion -- SAME artifact, no rebuild ---------
    def deploy_production(self, release_id: int) -> dict:
        r = self.get(release_id)
        if not r or r["status"] not in ("WAITING_PRODUCTION_APPROVAL", "TEST_VERIFIED"):
            return {"outcome": "BLOCKED", "release_id": release_id, "reason": "Release must reach WAITING_PRODUCTION_APPROVAL first"}
        # E10.17/E10.36: approval is required outright for CONTROLLED/
        # policy-forced/DESTRUCTIVE-or-UNKNOWN-migration releases -- but
        # the BINDING property (an approval is only valid for the exact
        # digest it was given for) holds whenever an approval was ever
        # actually recorded, even for a release that didn't strictly
        # require one (VIBE/AGENTIC_STANDARD may approve anyway; once
        # they do, a later rebuild must not silently reuse that stale
        # approval).
        approval_required = self.require_production_approval(release_id) or r["migration_classification"] in ("DESTRUCTIVE", "UNKNOWN")
        approval_given = bool(r["production_approved_by"])
        if (approval_required or approval_given) and not self._production_approval_valid(r):
            return {"outcome": "APPROVAL_REQUIRED", "release_id": release_id,
                     "reason": f"migration classification {r['migration_classification']}" if r["migration_classification"] in ("DESTRUCTIVE", "UNKNOWN") else None}
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (r["repository_id"],))
        previous = self.db.one(
            "SELECT * FROM deployments WHERE repository_id=? AND environment='PRODUCTION' AND status='VERIFIED' ORDER BY id DESC LIMIT 1",
            (r["repository_id"],))
        # No rebuild -- the SAME artifact identity BUILT once for TEST.
        deployment_id = self.db.execute(
            "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,artifact_version,artifact_image,"
            "artifact_digest,artifact_filename,artifact_sha256,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r["repository_id"], "PRODUCTION", repo["default_branch"], r["source_commit"], r["artifact_version"], r["artifact_image"],
             r["artifact_digest"], r["artifact_filename"], r["artifact_sha256"], "PENDING"))
        self._set(release_id, status="DEPLOYING_PRODUCTION", production_deployment_id=deployment_id)
        self.db.event("release", release_id, "PRODUCTION_DEPLOY_STARTED",
                        f"deployment={deployment_id} previous={previous['id'] if previous else None}")
        self.deployment_service.deploy(deployment_id)
        return {"outcome": "DEPLOYING", "release_id": release_id, "deployment_id": deployment_id,
                 "rollback_checkpoint_deployment_id": previous["id"] if previous else None}

    def sync_production_result(self, release_id: int) -> dict:
        r = self.get(release_id)
        if not r or not r["production_deployment_id"]:
            return {"outcome": "NO_PRODUCTION_DEPLOYMENT", "release_id": release_id}
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (r["production_deployment_id"],))
        if d["status"] in ("PENDING", "PREPARING", "BUILDING", "DEPLOYING", "VERIFYING"):
            return {"outcome": "DEPLOYING", "release_id": release_id, "deployment_status": d["status"]}
        wp_id = self._runtime_verification_wp(release_id, d["id"], "PRODUCTION")
        if d["status"] == "VERIFIED":
            self._set(release_id, status="PRODUCTION_VERIFIED", released_at=now())
            self.db.event("release", release_id, "RELEASE_COMPLETE", f"deployment={d['id']} work_product={wp_id}")
            return {"outcome": "RELEASE_COMPLETE", "release_id": release_id, "work_product_id": wp_id}
        self._set(release_id, status="FAILED", error=d["error"])
        self.db.event("release", release_id, "PRODUCTION_DEPLOY_FAILED", f"deployment={d['id']}")
        return {"outcome": "RUNTIME_VERIFY_FAILED", "release_id": release_id, "work_product_id": wp_id, "message": d["error"],
                 "rollback_available": True}

    # ---- E10.19/E10.20: rollback, delegated to DeploymentService --------
    def rollback_production(self, release_id: int) -> dict:
        r = self.get(release_id)
        if not r or not r["production_deployment_id"]:
            return {"outcome": "NO_PRODUCTION_DEPLOYMENT", "release_id": release_id}
        if r["migration_classification"] == "DESTRUCTIVE":
            # Never infer a safe rollback for a destructive migration --
            # the deploy itself already required human approval; an
            # automatic DB-state rollback is a SEPARATE, riskier
            # decision this service never makes unilaterally.
            return {"outcome": "ROLLBACK_REQUIRES_HUMAN", "release_id": release_id,
                     "reason": "DESTRUCTIVE migration classification -- confirm data/schema rollback safety manually"}
        ok, err, new_deployment_id = self.deployment_service.rollback(r["production_deployment_id"])
        if not ok:
            return {"outcome": "ROLLBACK_UNAVAILABLE", "release_id": release_id, "message": err}
        self.db.event("release", release_id, "RELEASE_ROLLBACK_STARTED", f"deployment={new_deployment_id}")
        return {"outcome": "ROLLING_BACK", "release_id": release_id, "deployment_id": new_deployment_id}

    def sync_rollback_result(self, release_id: int, deployment_id: int) -> dict:
        d = self.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        if not d:
            return {"outcome": "NOT_FOUND", "release_id": release_id}
        if d["status"] in ("PENDING", "PREPARING", "DEPLOYING", "VERIFYING"):
            return {"outcome": "ROLLING_BACK", "release_id": release_id}
        wp_id = self.work_products.create(
            kind="ROLLBACK_EVIDENCE", title=f"Rollback evidence (deployment {deployment_id})",
            status="APPROVED" if d["status"] == "ROLLED_BACK" else "REJECTED",
            content_metadata={"release_id": release_id, "deployment_id": deployment_id, "rollback_status": d["rollback_status"],
                                "restored_version": d["artifact_version"], "restored_digest": d["artifact_digest"] or d["artifact_sha256"],
                                "health_status": d["health_status"]})
        if d["status"] == "ROLLED_BACK":
            self._set(release_id, status="ROLLED_BACK")
            self.db.event("release", release_id, "RELEASE_ROLLED_BACK_VERIFIED", f"deployment={deployment_id} work_product={wp_id}")
            return {"outcome": "ROLLED_BACK_VERIFIED", "release_id": release_id, "work_product_id": wp_id}
        self.db.event("release", release_id, "RELEASE_ROLLBACK_FAILED", f"deployment={deployment_id}")
        return {"outcome": "ROLLBACK_FAILED", "release_id": release_id, "work_product_id": wp_id, "message": d["rollback_error"]}

    # ---- E10.23: WorkflowService.deploy_verified_gate ------------------
    def deploy_verified(self, change_id: int) -> bool | None:
        """None ("no Release evidence yet for this Change") tells
        WorkflowService to fall back to the legacy DEV-only check --
        see _gate_deploy_verified's own docstring."""
        task_ids = [t["id"] for t in self.db.all("SELECT id FROM tasks WHERE change_id=?", (change_id,))]
        if not task_ids:
            return None
        placeholders = ",".join("?" * len(task_ids))
        release_ids = [row["release_id"] for row in self.db.all(
            f"SELECT DISTINCT release_id FROM release_tasks WHERE task_id IN ({placeholders})", tuple(task_ids))]
        if not release_ids:
            return None
        for rid in release_ids:
            r = self.get(rid)
            if r and r["status"] == "PRODUCTION_VERIFIED":
                return True
        return False

    # ---- E10.30: explicit, single-step progression, never a daemon -----
    def release_tick(self, release_id: int, review_fix_orchestrator) -> dict:
        r = self.get(release_id)
        if not r:
            raise ReleaseError("Release not found")
        status = r["status"]
        if status == "DRAFT":
            return self.build(release_id)
        if status == "BUILT":
            return self.qualify(release_id, review_fix_orchestrator)
        if status == "READY":
            return self.deploy_test(release_id)
        if status == "DEPLOYING_TEST":
            return self.sync_test_result(release_id)
        if status == "TEST_VERIFIED":
            if self.require_production_approval(release_id) and not self._production_approval_valid(r):
                return {"outcome": "WAITING_PRODUCTION_APPROVAL", "release_id": release_id}
            return self.deploy_production(release_id)
        if status == "WAITING_PRODUCTION_APPROVAL":
            return self.deploy_production(release_id)
        if status == "DEPLOYING_PRODUCTION":
            return self.sync_production_result(release_id)
        return {"outcome": "NO_ELIGIBLE_STEP", "release_id": release_id, "status": status}
