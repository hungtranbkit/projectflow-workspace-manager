from __future__ import annotations
import json

"""Phase E12: Bug / Incident Closed Loop.

Production / User Feedback / Monitoring -> Incident/Bug -> Classify ->
Link existing Spec/Requirement -> Spec gap if needed -> Reproduce ->
Regression Test -> Plan/Fix -> Review -> Deploy -> Verify incident
resolved -> Close.

An Incident is a thin orchestration/tracking layer OVER a Change
(incidents.change_id) -- the exact same relationship ProductAcceptance
already has to Release/Deployment (E11): it never re-implements Spec/
Plan/Task/Review/Release/Deploy, it composes those existing services
and tracks only what is genuinely incident-specific:
  - classification/severity/source
  - the spec link (or spec-gap wait) that grounds the fix
  - real reproduction evidence
  - a regression test, real test_runs evidence recorded both BEFORE the
    fix (proving the bug) and AFTER the resolving deployment (proving
    the fix), never assumed
  - a final "verify incident resolved" check bound to the EXACT
    resolving Release/Deployment/source_commit (same artifact-binding
    discipline E11 used for ProductAcceptance -- stale evidence from a
    different commit never counts).

sync_status() is composition-only: it reads WorkflowService.
evaluate_workflow(change_id) and ReleaseService's own already-computed
state to advance FIX_PLANNED/FIX_REVIEWED/DEPLOYED, never a second,
independently-derived Task/Review/Release status."""

SOURCES = ("PRODUCTION", "USER_FEEDBACK", "MONITORING", "MANUAL")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
CLASSIFICATIONS = ("BUG", "REGRESSION", "SECURITY", "PERFORMANCE", "DATA_INTEGRITY", "UNKNOWN")
STATUSES = (
    "REPORTED", "CLASSIFIED", "SPEC_GAP_PENDING", "SPEC_LINKED",
    "REPRODUCING", "REPRODUCED", "CANNOT_REPRODUCE",
    "REGRESSION_TEST_ADDED", "FIX_PLANNED", "FIX_REVIEWED", "DEPLOYED",
    "VERIFIED", "VERIFICATION_FAILED", "CLOSED", "REOPENED",
)
_CLOSEABLE = ("VERIFIED", "CANNOT_REPRODUCE")
_CLASSIFICATION_CHANGE_TYPE = {"SECURITY": "SECURITY_CHANGE"}  # else BUG


class IncidentError(ValueError):
    pass


class IncidentService:
    def __init__(self, db, changes, work_products, trace, spec_lifecycle_service, test_case_specs,
                 workflow_service, release_service, specs_root):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.spec_lifecycle_service = spec_lifecycle_service
        self.test_case_specs = test_case_specs
        self.workflow_service = workflow_service
        self.release_service = release_service
        self.specs_root = specs_root

    # ---- reads ------------------------------------------------------
    def get(self, incident_id: int) -> dict | None:
        return self.db.one("SELECT * FROM incidents WHERE id=?", (incident_id,))

    def list(self, *, project_id: int | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM incidents WHERE 1=1", []
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(project_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC"
        return self.db.all(sql, tuple(args))

    def regression_history(self, incident_id: int) -> list[dict]:
        return self.db.all(
            "SELECT * FROM test_runs WHERE workspace_type='incident' AND workspace_id=? ORDER BY id", (incident_id,))

    def _require(self, incident_id: int) -> dict:
        row = self.get(incident_id)
        if not row:
            raise IncidentError("Incident not found")
        return row

    def _set(self, incident_id: int, **fields) -> dict:
        cols = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE incidents SET {cols},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (*fields.values(), incident_id))
        return self.get(incident_id)

    def _event(self, incident_id: int, action: str, details: str = "") -> None:
        self.db.event("incident", incident_id, action, details)

    # ---- E12: report --------------------------------------------------
    def report(self, title: str, description: str = "", source: str = "MANUAL", severity: str = "MEDIUM",
               reported_by: str = "system", project_id: int | None = None) -> dict:
        title = (title or "").strip()
        if not title:
            raise IncidentError("Incident title is required")
        source = (source or "MANUAL").strip().upper()
        if source not in SOURCES:
            raise IncidentError(f"Unknown source: {source} (must be one of {SOURCES})")
        severity = (severity or "MEDIUM").strip().upper()
        if severity not in SEVERITIES:
            raise IncidentError(f"Unknown severity: {severity} (must be one of {SEVERITIES})")
        if project_id is not None and not self.db.one("SELECT id FROM repositories WHERE id=?", (project_id,)):
            raise IncidentError(f"Unknown project_id: {project_id}")
        incident_id = self.db.execute(
            "INSERT INTO incidents(project_id,title,description,source,severity,reported_by) VALUES(?,?,?,?,?,?)",
            (project_id, title, (description or "").strip(), source, severity, (reported_by or "system").strip()))
        wp_id = self.work_products.create(
            kind="INCIDENT_REPORT", title=f"Incident: {title}", project_id=project_id,
            status="DRAFT", content_metadata={"incident_id": incident_id, "source": source, "severity": severity})
        self._set(incident_id, work_product_id=wp_id)
        self._event(incident_id, "INCIDENT_REPORTED", f"source={source} severity={severity}")
        return self.get(incident_id)

    # ---- E12: classify -- also materializes the fix Change, the one
    # carrier every remaining step (spec/plan/review/release) hangs off. --
    def classify(self, incident_id: int, classification: str, severity: str | None = None) -> dict:
        row = self._require(incident_id)
        if row["status"] not in ("REPORTED", "CLASSIFIED", "REOPENED"):
            raise IncidentError(f"Incident is {row['status']} -- cannot (re)classify now")
        classification = (classification or "").strip().upper()
        if classification not in CLASSIFICATIONS:
            raise IncidentError(f"Unknown classification: {classification} (must be one of {CLASSIFICATIONS})")
        fields = {"classification": classification, "status": "CLASSIFIED"}
        if severity:
            severity = severity.strip().upper()
            if severity not in SEVERITIES:
                raise IncidentError(f"Unknown severity: {severity} (must be one of {SEVERITIES})")
            fields["severity"] = severity
        if not row["change_id"]:
            change_type = _CLASSIFICATION_CHANGE_TYPE.get(classification, "BUG")
            change_id = self.changes.create(
                title=f"Fix: {row['title']}", description=row["description"], change_type=change_type,
                risk_level="HIGH" if (fields.get("severity") or row["severity"]) in ("HIGH", "CRITICAL") else "NORMAL",
                project_id=row["project_id"])
            fields["change_id"] = change_id
        updated = self._set(incident_id, **fields)
        self._event(incident_id, "INCIDENT_CLASSIFIED", f"classification={classification} change_id={updated['change_id']}")
        return updated

    # ---- E12: link existing Spec/Requirement -- reuses E6's own
    # GOVERNED_BY trace link, so ArchitectureContextBuilder/TestDesign
    # coverage pick this Change up for free, exactly like any other. ----
    def link_spec(self, incident_id: int, feature_id: str, requirement_ids=(), acceptance_ids=()) -> dict:
        row = self._require(incident_id)
        if not row["change_id"]:
            raise IncidentError("Classify the incident (materializing its fix Change) before linking a spec")
        from app.services.spec_registry import SpecRegistry, SpecError
        try:
            registry = SpecRegistry(self.specs_root).load()
        except SpecError as exc:
            raise IncidentError(f"Spec registry unavailable: {exc}") from exc
        feature = registry.feature(feature_id)
        if not feature:
            raise IncidentError(f"Unknown spec feature: {feature_id}")
        self.trace.link("change", row["change_id"], "spec_feature", feature_id, relation="GOVERNED_BY")
        updated = self._set(
            incident_id, spec_feature_id=feature_id, status="SPEC_LINKED",
            requirement_ids=json.dumps(list(requirement_ids)), acceptance_ids=json.dumps(list(acceptance_ids)))
        self._event(incident_id, "INCIDENT_SPEC_LINKED", feature_id)
        return updated

    def mark_spec_gap(self, incident_id: int, note: str = "") -> dict:
        row = self._require(incident_id)
        if not row["change_id"]:
            raise IncidentError("Classify the incident before raising a spec gap")
        updated = self._set(incident_id, status="SPEC_GAP_PENDING")
        self._event(incident_id, "INCIDENT_SPEC_GAP_RAISED", note)
        return updated

    def sync_spec_gap(self, incident_id: int) -> dict:
        """Composition only -- reuses SpecLifecycleService's own real
        proposal state, never a second spec-review calculation. Auto-
        advances SPEC_GAP_PENDING -> SPEC_LINKED once any proposal for
        the fix Change has actually been APPLIED (the same real spec
        baseline write E5 already performs)."""
        row = self._require(incident_id)
        if row["status"] != "SPEC_GAP_PENDING" or not row["change_id"] or not self.spec_lifecycle_service:
            return row
        applied = [p for p in self.spec_lifecycle_service.list_proposals(row["change_id"]) if p["status"] == "APPLIED"]
        if not applied:
            return row
        latest = applied[-1]
        updated = self._set(incident_id, status="SPEC_LINKED", spec_gap_proposal_id=latest["id"],
                             spec_feature_id=latest["feature_id"])
        self._event(incident_id, "INCIDENT_SPEC_GAP_RESOLVED", f"proposal={latest['id']}")
        return updated

    # ---- E12: reproduce -------------------------------------------------
    def start_reproduction(self, incident_id: int) -> dict:
        row = self._require(incident_id)
        if row["status"] not in ("CLASSIFIED", "SPEC_LINKED", "REOPENED"):
            raise IncidentError(f"Incident is {row['status']} -- classify/link a spec before reproducing")
        updated = self._set(incident_id, status="REPRODUCING")
        self._event(incident_id, "INCIDENT_REPRODUCTION_STARTED")
        return updated

    def record_reproduction(self, incident_id: int, reproduced: bool, note: str = "", commit: str | None = None) -> dict:
        row = self._require(incident_id)
        if row["status"] != "REPRODUCING":
            raise IncidentError(f"Incident is {row['status']}, not REPRODUCING")
        status = "REPRODUCED" if reproduced else "CANNOT_REPRODUCE"
        updated = self._set(incident_id, status=status, reproduction_note=(note or "").strip(), reproduced_commit=commit)
        self._event(incident_id, "INCIDENT_" + status, note)
        return updated

    # ---- E12: regression test -- reuses TestCaseSpecStore, never a
    # second test-case model; real test_runs evidence, never assumed. ----
    def add_regression_test(self, incident_id: int, test_case_spec_id: int) -> dict:
        row = self._require(incident_id)
        if row["status"] != "REPRODUCED":
            raise IncidentError(f"Incident is {row['status']}, not REPRODUCED -- reproduce it first")
        tc = self.test_case_specs.get(test_case_spec_id) if self.test_case_specs else None
        if not tc or tc["change_id"] != row["change_id"]:
            raise IncidentError("test_case_spec_id must reference a real TestCaseSpec on this incident's own fix Change")
        updated = self._set(incident_id, status="REGRESSION_TEST_ADDED", regression_test_case_spec_id=test_case_spec_id)
        self._event(incident_id, "INCIDENT_REGRESSION_TEST_ADDED", f"test_case_spec={test_case_spec_id}")
        return updated

    def record_regression_result(self, incident_id: int, status: str, tested_commit: str, command: str = "") -> dict:
        """Real evidence into the EXISTING test_runs table (workspace_
        type='incident' -- a new value, safe: every existing query
        filters by an exact literal workspace_type, see
        evidence_store.py). Called at least twice across a real closed
        loop: once at the reproduction commit (expected FAIL, proving
        the bug), once at the resolving deployment's own source_commit
        (expected PASS, proving the fix -- see verify_resolved)."""
        row = self._require(incident_id)
        if not row["regression_test_case_spec_id"]:
            raise IncidentError("No regression test attached to this incident yet")
        status = (status or "").strip().upper()
        if status not in ("PASS", "FAIL"):
            raise IncidentError("status must be PASS or FAIL")
        # test_case_spec_id set directly in the INSERT -- Database.
        # execute() opens a fresh connection per call (app/db.py), so a
        # follow-up UPDATE keyed on SQL last_insert_rowid() would read
        # that NEW connection's own (empty) insert history, not the
        # INSERT above's.
        self.db.execute(
            "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,test_case_spec_id,started_at,finished_at) "
            "VALUES('incident',?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (incident_id, command or f"regression test for incident {incident_id}", "REGRESSION", status, tested_commit,
             row["regression_test_case_spec_id"]))
        self._event(incident_id, "INCIDENT_REGRESSION_RESULT", f"{status}@{tested_commit}")
        return self.get(incident_id)

    # ---- E12: plan/fix, review, deploy -- composition only, reads the
    # fix Change's OWN already-computed real state, never re-derived. ----
    def sync_status(self, incident_id: int) -> dict:
        row = self._require(incident_id)
        if row["status"] not in ("REGRESSION_TEST_ADDED", "FIX_PLANNED", "FIX_REVIEWED", "DEPLOYED"):
            return row
        change_id = row["change_id"]
        if not change_id:
            return row
        tasks = self.changes.list_tasks_for_change(change_id)
        new_status = row["status"]
        if tasks and row["status"] == "REGRESSION_TEST_ADDED":
            new_status = "FIX_PLANNED"
        state = self.workflow_service.evaluate_workflow(change_id) if self.workflow_service and tasks else None
        if state:
            review_stage = next((s for s in state["stages"] if s["stage"] == "REVIEW"), None)
            # E12's own "Review" step maps to REVIEW_PASS specifically
            # (independent Code Review, E9) -- not the whole REVIEW
            # stage's bundled SPEC_COMPLIANCE_PASS/SECURITY_PASS, which
            # are already-existing, separately-tracked E5/E9 concerns
            # this incident's own fix Change carries independently.
            if review_stage and review_stage["gates"].get("REVIEW_PASS") and new_status in ("FIX_PLANNED",):
                new_status = "FIX_REVIEWED"
        release = self._current_release(change_id)
        if release and release["status"] == "PRODUCTION_VERIFIED" and new_status in ("FIX_PLANNED", "FIX_REVIEWED"):
            new_status = "DEPLOYED"
        if new_status != row["status"]:
            fields = {"status": new_status}
            if new_status == "DEPLOYED" and release:
                fields["resolved_release_id"] = release["id"]
                fields["resolved_deployment_id"] = release.get("production_deployment_id")
            row = self._set(incident_id, **fields)
            self._event(incident_id, "INCIDENT_STATUS_SYNCED", new_status)
        return row

    def _current_release(self, change_id: int) -> dict | None:
        """Same 'newest Release among this Change's Tasks' join E11's
        ProductAcceptanceService already established."""
        if not self.release_service:
            return None
        task_ids = [t["id"] for t in self.changes.list_tasks_for_change(change_id)]
        if not task_ids:
            return None
        placeholders = ",".join("?" * len(task_ids))
        rows = self.db.all(f"SELECT DISTINCT release_id FROM release_tasks WHERE task_id IN ({placeholders})", tuple(task_ids))
        for rid in sorted((r["release_id"] for r in rows), reverse=True):
            r = self.release_service.get(rid)
            if r:
                return r
        return None

    # ---- E12: verify incident resolved -- artifact-bound, never a
    # default PASS merely because a deployment is healthy. ---------------
    def verify_resolved(self, incident_id: int) -> dict:
        row = self._require(incident_id)
        if row["status"] not in ("DEPLOYED", "VERIFICATION_FAILED"):
            raise IncidentError(f"Incident is {row['status']}, not DEPLOYED -- sync_status() once a Release is production-verified")
        if not row["resolved_release_id"] or not row["resolved_deployment_id"]:
            raise IncidentError("No resolving Release/Deployment recorded yet")
        release = self.release_service.get(row["resolved_release_id"]) if self.release_service else None
        if not release or release["status"] != "PRODUCTION_VERIFIED":
            raise IncidentError("Resolving Release is not (or no longer) PRODUCTION_VERIFIED")
        latest = self.db.one(
            "SELECT * FROM test_runs WHERE workspace_type='incident' AND workspace_id=? "
            "AND test_case_spec_id=? ORDER BY id DESC LIMIT 1",
            (incident_id, row["regression_test_case_spec_id"]))
        if not latest or latest["status"] != "PASS" or latest["tested_commit"] != release["source_commit"]:
            updated = self._set(incident_id, status="VERIFICATION_FAILED")
            self._event(incident_id, "INCIDENT_VERIFICATION_FAILED",
                        "No PASS regression test result at the resolving Release's own source_commit")
            return updated
        updated = self._set(incident_id, status="VERIFIED", verified_at=_now(), verification_note=f"release={release['id']} commit={release['source_commit']}")
        self._record_evidence(updated, verdict="VERIFIED")
        self._event(incident_id, "INCIDENT_VERIFIED", f"release={release['id']}")
        return self.get(incident_id)  # _record_evidence updates work_product_id -- return the fresh row

    # ---- E12: close ------------------------------------------------------
    def close(self, incident_id: int, closed_by: str, note: str = "") -> dict:
        row = self._require(incident_id)
        if row["status"] not in _CLOSEABLE:
            raise IncidentError(f"Incident is {row['status']} -- must be VERIFIED or CANNOT_REPRODUCE to close")
        updated = self._set(incident_id, status="CLOSED", closed_at=_now(), closed_by=(closed_by or "").strip())
        self._record_evidence(updated, verdict="CLOSED", note=note)
        self._event(incident_id, "INCIDENT_CLOSED", f"by={closed_by}")
        return self.get(incident_id)

    def reopen(self, incident_id: int, reason: str) -> dict:
        row = self._require(incident_id)
        if row["status"] not in ("CLOSED", "VERIFIED", "VERIFICATION_FAILED"):
            raise IncidentError(f"Incident is {row['status']} -- nothing to reopen")
        reason = (reason or "").strip()
        if not reason:
            raise IncidentError("A reason is required to reopen an incident")
        updated = self._set(incident_id, status="REOPENED", closed_at=None, closed_by=None,
                             verified_at=None, resolved_release_id=None, resolved_deployment_id=None)
        self._record_evidence(updated, verdict="REOPENED", note=reason)
        self._event(incident_id, "INCIDENT_REOPENED", reason)
        return self.get(incident_id)

    def _record_evidence(self, row: dict, verdict: str, note: str = "") -> None:
        wp_id = self.work_products.create(
            kind="INCIDENT_REPORT", title=f"Incident: {row['title']} ({verdict})",
            project_id=row["project_id"], change_id=row["change_id"], status="APPROVED" if verdict in ("VERIFIED", "CLOSED") else "DRAFT",
            supersedes_id=row.get("work_product_id"),
            content_metadata={"incident_id": row["id"], "status": row["status"], "verdict": verdict, "note": note,
                               "classification": row["classification"], "severity": row["severity"],
                               "resolved_release_id": row["resolved_release_id"], "resolved_deployment_id": row["resolved_deployment_id"],
                               "regression_test_case_spec_id": row["regression_test_case_spec_id"]})
        self.db.execute("UPDATE incidents SET work_product_id=? WHERE id=?", (wp_id, row["id"]))


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
