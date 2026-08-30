from __future__ import annotations
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

"""Autonomous Spec Lifecycle (Phase E5): Human Intent/Change ->
Requirement Analysis -> Spec Draft -> Spec Review -> Refinement -> Spec
Ready -> Apply, with a hard WHAT-vs-HOW boundary.

CRITICAL ARCHITECTURAL RULE, enforced by construction across four
separate classes, mirroring E4's own Planner/Plan/Materialize/Execute
separation exactly:

  SPEC AUTHORING AGENT  -- SpecAuthorService. One bounded, stateless,
    tool-less PlannerAgentInvoker.invoke() call (same shared mechanism
    E4 uses) that returns STRUCTURED data, never asked to hand-write
    the canonical YAML itself -- this module serializes deterministically
    (_serialize_feature_yaml) after the LLM's output is already a plain
    Python dict.

  SPEC ARTIFACT          -- spec_proposals (+ SpecProposalValidator).
    A proposed FeatureSpec revision, separate from the canonical
    specs/ tree until explicitly APPLIED. Nothing here ever writes to
    specs/ before a proposal reaches READY.

  SPEC REVIEW AGENT      -- SpecReviewService. A COMPLETELY SEPARATE
    invoke() call (a fresh, brand-new subprocess -- PlannerAgentInvoker
    has no session/conversation state at all) that receives only the
    proposal's final structured content, never the author's own
    rationale/reasoning text -- see review()'s context builder, which
    deliberately omits any "rationale"-shaped field.

  SPEC APPROVAL / ACTIVATION -- SpecLifecycleService.apply_proposal().
    The ONLY place canonical specs/**/*.yaml is ever written by
    ProjectFlow code, atomically, and only for a READY proposal, with a
    real SpecRegistry.load() re-validation before AND after the write
    (staged validation before, real confirmation after) so a broken
    write can never leave the approved baseline invalid.

SpecRegistry (app/services/spec_registry.py) remains the ONE canonical
truth loader throughout -- every validation step here re-runs the real
SpecRegistry against a staged/temp copy of specs/, never a duplicated
parallel schema check."""

import yaml

from app.services.human_decisions import HumanDecisionService

SPEC_PROPOSAL_STATUSES = ("DRAFT", "REVIEWING", "NEEDS_REFINEMENT", "HUMAN_DECISION_REQUIRED", "READY", "APPLIED", "REJECTED", "SUPERSEDED")
SPEC_CHANGE_SIGNALS = ("AUTO_SPEC_REFINEMENT", "HUMAN_SPEC_CHANGE_REQUIRED", "HOW_DECISION", "NONE")


class SpecLifecycleError(ValueError):
    pass


def _id_block(kind: str) -> dict:
    return {"type": "array", "items": {"type": "object", "required": ["id", "text"],
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}


# ===================================================================
# Structured output schemas -- each role gets its OWN schema, never a
# shared/global one (matches PlannerAgentInvoker.invoke()'s own contract).
# ===================================================================
REQUIREMENT_ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "required": ["problem_statement", "functional_requirements"],
    "properties": {
        "problem_statement": {"type": "string"},
        "user_goals": {"type": "array", "items": {"type": "string"}},
        "actors": {"type": "array", "items": {"type": "string"}},
        "functional_requirements": {"type": "array", "items": {"type": "string"}},
        "non_functional_requirements": {"type": "array", "items": {"type": "string"}},
        "business_rules": {"type": "array", "items": {"type": "string"}},
        "edge_cases": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["issue", "classification"],
                "properties": {
                    "issue": {"type": "string"},
                    "classification": {"type": "string", "enum": list(SPEC_CHANGE_SIGNALS)},
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "affected_existing_features": {"type": "array", "items": {"type": "string"}},
        "candidate_new_features": {"type": "array", "items": {"type": "string"}},
    },
}

SPEC_PROPOSAL_JSON_SCHEMA = {
    "type": "object",
    "required": ["feature_id", "title", "requirements", "acceptance_criteria"],
    "properties": {
        "feature_id": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "scope_includes": {"type": "array", "items": {"type": "string"}},
        "scope_excludes": {"type": "array", "items": {"type": "string"}},
        "requirements": _id_block("requirements"),
        "acceptance_criteria": _id_block("acceptance_criteria"),
        "invariants": _id_block("invariants"),
    },
}

SPEC_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "NEEDS_REFINEMENT", "HUMAN_DECISION_REQUIRED", "REJECT"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["category", "description"],
                "properties": {
                    "category": {"type": "string", "enum": [
                        "completeness", "ambiguity", "contradiction", "business_logic", "edge_case",
                        "testability", "invariant_quality", "scope_creep", "compatibility", "architecture_conflict"]},
                    "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                    "description": {"type": "string"},
                    "classification": {"type": "string", "enum": list(SPEC_CHANGE_SIGNALS)},
                    "suggested_fix": {"type": "string"},
                },
            },
        },
        "human_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "reason"],
                "properties": {
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                    "spec_change_signal": {"type": "string", "enum": list(SPEC_CHANGE_SIGNALS)},
                },
            },
        },
    },
}

REQUIREMENTS_ANALYST_PREAMBLE = """You are the REQUIREMENTS ANALYST for ProjectFlow, an autonomous software engineering control plane.

Core principles: Human owns intent -- turn the raw intent below into a structured, testable requirement analysis without changing what was asked. Spec owns expected behavior; you feed the Spec Layer, you don't skip it.

For every open question or ambiguity, classify it as exactly one of:
- HOW_DECISION: an implementation choice (library, internal structure, algorithm, file layout) with no user-visible behavior difference. Never escalate these -- resolve them yourself or omit them.
- AUTO_SPEC_REFINEMENT: a concrete detail directly implied by the stated intent (an edge case, a derived validation rule, a failure state needed to make the intent testable) that does not change what the user asked for.
- HUMAN_SPEC_CHANGE_REQUIRED: continuing would require CHOOSING between materially different user-facing outcomes, business rules, permissions, data meaning, or security boundaries -- never guess these.

Be concrete and specific. Do not pad with generic boilerplate."""

SPEC_ANALYST_PREAMBLE = """You are the SPEC ANALYST for ProjectFlow, an autonomous software engineering control plane.

Author a FeatureSpec candidate strictly from the Requirement Analysis and approved context below -- never invent scope beyond it, never weaken or omit a requirement the analysis already established.

Rules:
- feature_id: if updating an existing feature, reuse its EXACT existing id; if new, propose a stable "FEAT-<SOMETHING>" id.
- Give every requirement/acceptance_criterion/invariant a short, stable local id (e.g. "REQ-001", "AC-001", "INV-001") -- ProjectFlow will remap it deterministically if it collides with an existing id elsewhere; you do not need to check global uniqueness yourself.
- Every acceptance criterion should be concretely verifiable (an observable outcome, not a vague aspiration).
- Every requirement should map to at least one acceptance criterion where practical.
- Invariants are properties that must hold at all times, not just at completion.
- If resolved human decisions are provided below, incorporate their answers verbatim -- do not re-litigate them."""

SPEC_REVIEWER_PREAMBLE = """You are an INDEPENDENT SPEC REVIEWER for ProjectFlow. You are a fresh reviewer with no memory of how this proposal was authored -- judge only what is in front of you.

Review the proposed FeatureSpec below against the original human intent, the Requirement Analysis, and existing related specs. Check explicitly for:
- completeness (every stated requirement covered), ambiguity, internal contradiction
- business logic soundness, edge cases, testability (can each acceptance criterion actually be verified? is anything vague, has hidden timing assumptions, or an unstated precondition?)
- invariant quality (do they actually hold, do they contradict a requirement?)
- scope creep beyond the original intent, compatibility with existing approved specs, likely architecture conflicts

Verdict:
- PASS only if there is nothing worth fixing.
- NEEDS_REFINEMENT for concrete, fixable quality/completeness/testability issues -- describe exactly what to fix.
- HUMAN_DECISION_REQUIRED only if a finding requires choosing between materially different user-facing outcomes (do not use this for ordinary spec-writing quality issues).
- REJECT only if the proposal is fundamentally unusable as written.

Classify each finding as HOW_DECISION / AUTO_SPEC_REFINEMENT / HUMAN_SPEC_CHANGE_REQUIRED where applicable, same definitions as the Requirements Analyst uses. Never suggest weakening or deleting a requirement/acceptance criterion/invariant merely to make review easier."""


def _serialize_feature_yaml(content: dict, version: int, status: str = "approved") -> str:
    """The ONE place a FeatureSpec dict becomes canonical YAML text --
    the exact shape every hand-written specs/features/*.yaml already
    uses (schema_version/id/title/version/status/summary/scope/
    requirements/acceptance_criteria/invariants)."""
    data = {
        "schema_version": 1,
        "id": content["feature_id"],
        "title": content.get("title") or content["feature_id"],
        "version": version,
        "status": status,
        "summary": content.get("summary") or "",
        "scope": {
            "includes": content.get("scope_includes") or [],
            "excludes": content.get("scope_excludes") or [],
        },
        "requirements": content.get("requirements") or [],
        "acceptance_criteria": content.get("acceptance_criteria") or [],
        "invariants": content.get("invariants") or [],
    }
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=100, allow_unicode=True)


def _resolve_id_collisions(items: list[dict], registry_ids: dict, feature_id: str) -> tuple[list[dict], list[dict]]:
    """E5.5: never let the LLM accidentally collide with an existing
    global id. `registry_ids` maps id -> _feature_id for one id kind
    (requirements/acceptance/invariants). A collision against the SAME
    feature (a legitimate update reusing its own stable id) is fine and
    never remapped; a collision against a DIFFERENT feature, or a
    duplicate within this same proposal, is deterministically
    remapped with a preserved audit note -- never silently overwriting
    another feature's definition."""
    used: set[str] = set()
    notes: list[dict] = []
    out: list[dict] = []
    for it in items or []:
        rid = (it.get("id") or "").strip()
        if not rid:
            continue
        owner = registry_ids.get(rid)
        collides = (owner is not None and owner != feature_id) or rid in used
        new_id = rid
        if collides:
            n = 2
            while f"{rid}-{n}" in registry_ids or f"{rid}-{n}" in used:
                n += 1
            new_id = f"{rid}-{n}"
            notes.append({"original": rid, "remapped": new_id,
                          "reason": "collided with an existing id under a different feature" if owner else "duplicate id within this proposal"})
        used.add(new_id)
        out.append({"id": new_id, "text": (it.get("text") or "").strip()})
    return out, notes


class _AgentRole:
    """Shared plumbing for every E5 agent role -- resolves the real
    RoleCapabilityService assignment before invoking, exactly like
    PlannerService already does for PLANNER (E4.3/E5's own 'the same
    RoleCapabilityService, not a second validator')."""

    def __init__(self, db, invoker, roles_catalog, role_key):
        self.db = db
        self.invoker = invoker
        self.roles_catalog = roles_catalog
        self.role_key = role_key

    def _check_assignment(self, provider):
        assignment = self.roles_catalog.validate_assignment(provider, self.role_key)
        if not assignment["valid"]:
            raise SpecLifecycleError(f"Provider '{provider}' cannot act as {self.role_key}: missing {assignment['missing_required_capabilities']}")


# ===================================================================
# Requirement Analysis (E5.2/E5.3)
# ===================================================================
class RequirementAnalysisService(_AgentRole):
    def __init__(self, db, changes, work_products, invoker, roles_catalog, specs_root, repo_root):
        super().__init__(db, invoker, roles_catalog, "REQUIREMENTS_ANALYST")
        self.changes = changes
        self.work_products = work_products
        self.specs_root = specs_root
        self.repo_root = repo_root

    def _build_context(self, change_id: int) -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError
        change = self.changes.get(change_id)
        spec_block = {"approved_features": []}
        glossary = {}
        try:
            registry = SpecRegistry(self.specs_root).load()
            spec_block["approved_features"] = [
                {"id": f["id"], "title": f.get("title"), "summary": f.get("summary")}
                for f in registry.features.values() if f.get("status") == "approved"
            ]
            glossary_path = Path(self.specs_root) / (registry.manifest.get("glossary") or "glossary.yaml")
            if glossary_path.is_file():
                glossary = yaml.safe_load(glossary_path.read_text()) or {}
        except SpecError:
            pass
        return {
            "change": {"id": change_id, "title": change.get("title") if change else None,
                       "original_intent": change.get("description") if change else None,
                       "change_type": change.get("change_type") if change else None,
                       "risk_level": change.get("risk_level") if change else None},
            "existing_approved_specs": spec_block["approved_features"],
            "glossary": glossary.get("terms") or {},
            "existing_work_products": [
                {"kind": wp["kind"], "title": wp["title"], "status": wp["status"]}
                for wp in self.work_products.list_for_change(change_id)
            ],
        }

    def analyze(self, change_id: int, provider: str = "claude") -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise SpecLifecycleError("Change not found")
        try:
            self._check_assignment(provider)
        except SpecLifecycleError as exc:
            self.db.event("change", change_id, "REQUIREMENT_ANALYSIS_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "work_product": None, "message": str(exc)}

        context = self._build_context(change_id)
        prompt = REQUIREMENTS_ANALYST_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        try:
            raw_text = self.invoker.invoke(provider, prompt, REQUIREMENT_ANALYSIS_JSON_SCHEMA, self.repo_root)
        except Exception as exc:
            self.db.event("change", change_id, "REQUIREMENT_ANALYSIS_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "work_product": None, "message": str(exc)}
        try:
            parsed = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            self.db.event("change", change_id, "REQUIREMENT_ANALYSIS_OUTPUT_INVALID", str(exc))
            return {"outcome": "OUTPUT_INVALID", "work_product": None, "message": f"Not valid JSON: {exc}"}
        if not isinstance(parsed, dict) or "problem_statement" not in parsed or "functional_requirements" not in parsed:
            self.db.event("change", change_id, "REQUIREMENT_ANALYSIS_OUTPUT_INVALID", "missing required fields")
            return {"outcome": "OUTPUT_INVALID", "work_product": None, "message": "Missing problem_statement/functional_requirements"}

        wp_id = self.work_products.create(
            kind="REQUIREMENT_ANALYSIS", title=f"Requirement Analysis for {change['title']}",
            project_id=change.get("project_id"), change_id=change_id, status="DRAFT",
            content_metadata=parsed)
        hd_ids = []
        for amb in parsed.get("ambiguities") or []:
            if amb.get("classification") == "HUMAN_SPEC_CHANGE_REQUIRED":
                hd = HumanDecisionService(self.db).create(
                    "change", change_id, amb.get("question") or amb.get("issue") or "",
                    amb.get("reason") or "", "HUMAN_SPEC_CHANGE_REQUIRED")
                hd_ids.append(hd)
        # E5.20: a lightweight, traceable system-executed Task -- never a
        # Builder Workspace/AgentSession (nothing here calls
        # _start_builder_session). Known limitation, stated plainly: this
        # Task's own TaskDecisionService status stays whatever a plain
        # BACKLOG->ACTIVE Task with no Builder Workspace shows (there is
        # no "system already completed this" concept in that engine yet)
        # -- the real, load-bearing evidence is the WorkProduct link.
        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"reqanalysis-{change_id}-{wp_id}", f"Requirement Analysis: {change['title']}", "",
             "ACTIVE", change_id, "REQUIREMENT_ANALYSIS"))
        self.work_products.link_task(task_id, wp_id, "OUTPUT")
        self.db.event("change", change_id, "REQUIREMENT_ANALYSIS_CREATED", f"work_product={wp_id} human_decisions={len(hd_ids)}")
        return {"outcome": "READY", "work_product": self.work_products.get(wp_id), "human_decision_ids": hd_ids, "task_id": task_id}


# ===================================================================
# Spec Authoring (E5.4/E5.5)
# ===================================================================
class SpecAuthorService(_AgentRole):
    def __init__(self, db, changes, work_products, invoker, roles_catalog, specs_root, repo_root):
        super().__init__(db, invoker, roles_catalog, "SPEC_ANALYST")
        self.changes = changes
        self.work_products = work_products
        self.specs_root = specs_root
        self.repo_root = repo_root

    def get(self, proposal_id: int) -> dict | None:
        return self.db.one("SELECT * FROM spec_proposals WHERE id=?", (proposal_id,))

    def list_for_change(self, change_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM spec_proposals WHERE change_id=? ORDER BY id", (change_id,))

    def _invoke_and_parse(self, provider, prompt) -> dict:
        raw_text = self.invoker.invoke(provider, prompt, SPEC_PROPOSAL_JSON_SCHEMA, self.repo_root)
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict) or "feature_id" not in parsed or "requirements" not in parsed:
            raise SpecLifecycleError("Spec author output missing feature_id/requirements")
        return parsed

    def _store_proposal(self, change_id, provider, parsed, digest, requirement_analysis_wp_id, supersedes_id, round_no) -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError
        change = self.changes.get(change_id)
        feature_id = (parsed.get("feature_id") or "").strip()
        if not feature_id:
            raise SpecLifecycleError("Spec author output has an empty feature_id")

        registry_requirements: dict[str, str] = {}
        registry_acceptance: dict[str, str] = {}
        registry_invariants: dict[str, str] = {}
        base_version = None
        try:
            registry = SpecRegistry(self.specs_root).load()
            registry_requirements = {k: v["_feature_id"] for k, v in registry.requirements.items()}
            registry_acceptance = {k: v["_feature_id"] for k, v in registry.acceptance.items()}
            registry_invariants = {k: v["_feature_id"] for k, v in registry.invariants.items()}
            existing = registry.feature(feature_id)
            if existing:
                base_version = existing.get("version")
        except SpecError:
            pass

        reqs, req_notes = _resolve_id_collisions(parsed.get("requirements"), registry_requirements, feature_id)
        accs, acc_notes = _resolve_id_collisions(parsed.get("acceptance_criteria"), registry_acceptance, feature_id)
        invs, inv_notes = _resolve_id_collisions(parsed.get("invariants"), registry_invariants, feature_id)
        notes = req_notes + acc_notes + inv_notes

        proposed_version = (base_version or 0) + 1
        content = {"feature_id": feature_id, "title": parsed.get("title") or feature_id,
                   "summary": parsed.get("summary") or "", "scope_includes": parsed.get("scope_includes") or [],
                   "scope_excludes": parsed.get("scope_excludes") or [], "requirements": reqs,
                   "acceptance_criteria": accs, "invariants": invs}

        proposal_id = self.db.execute(
            "INSERT INTO spec_proposals(change_id,project_id,feature_id,base_spec_version,proposed_version,status,author_provider,author_role,input_context_digest,proposed_content,refinement_round,id_remap_notes,supersedes_proposal_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (change_id, change.get("project_id") if change else None, feature_id, base_version, proposed_version,
             "DRAFT", provider, "SPEC_ANALYST", digest, json.dumps(content), round_no, json.dumps(notes), supersedes_id))
        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"specauthor-{change_id}-{proposal_id}", f"Spec Authoring: {feature_id} v{proposed_version}", "",
             "ACTIVE", change_id, "SPEC_AUTHORING"))
        self.db.event("change", change_id, "SPEC_PROPOSAL_CREATED", f"proposal={proposal_id} feature={feature_id} v{proposed_version} round={round_no}")
        return {"proposal": self.get(proposal_id), "task_id": task_id}

    def author(self, change_id: int, requirement_analysis_wp_id: int, provider: str = "claude") -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise SpecLifecycleError("Change not found")
        wp = self.work_products.get(requirement_analysis_wp_id)
        if not wp or wp["kind"] != "REQUIREMENT_ANALYSIS":
            raise SpecLifecycleError("requirement_analysis_wp_id does not reference a REQUIREMENT_ANALYSIS WorkProduct")
        try:
            self._check_assignment(provider)
        except SpecLifecycleError as exc:
            self.db.event("change", change_id, "SPEC_AUTHOR_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "proposal": None, "message": str(exc)}

        from app.services.spec_registry import SpecRegistry, SpecError
        approved_related = []
        try:
            registry = SpecRegistry(self.specs_root).load()
            approved_related = [{"id": f["id"], "title": f.get("title"), "version": f.get("version"),
                                  "requirements": f.get("requirements"), "acceptance_criteria": f.get("acceptance_criteria")}
                                 for f in registry.features.values() if f.get("status") == "approved"]
        except SpecError:
            pass
        resolved_decisions = [d for d in HumanDecisionService(self.db).list_for("change", change_id) if d["resolved"]]
        context = {"change_intent": change.get("description") or change.get("title"),
                   "requirement_analysis": json.loads(wp["content_metadata"] or "{}"),
                   "approved_related_specs": approved_related,
                   "resolved_human_decisions": [{"question": d["question"], "resolution": d["resolution_note"]} for d in resolved_decisions]}
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = SPEC_ANALYST_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        try:
            parsed = self._invoke_and_parse(provider, prompt)
        except Exception as exc:
            self.db.event("change", change_id, "SPEC_AUTHOR_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED" if "missing" not in str(exc).lower() else "OUTPUT_INVALID", "proposal": None, "message": str(exc)}

        stored = self._store_proposal(change_id, provider, parsed, digest, requirement_analysis_wp_id, None, 0)
        return {"outcome": "READY", **stored}

    def refine(self, proposal_id: int, review_findings: dict, provider: str = "claude") -> dict:
        prior = self.get(proposal_id)
        if not prior:
            raise SpecLifecycleError("Proposal not found")
        try:
            self._check_assignment(provider)
        except SpecLifecycleError as exc:
            self.db.event("change", prior["change_id"], "SPEC_REFINE_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "proposal": None, "message": str(exc)}

        context = {"prior_proposal": json.loads(prior["proposed_content"]), "review_findings": review_findings}
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = (SPEC_ANALYST_PREAMBLE +
                  "\n\nYou are REFINING a prior proposal based on independent review findings below. "
                  "Address every AUTO_SPEC_REFINEMENT and NEEDS_REFINEMENT finding. Never remove a "
                  "requirement/acceptance criterion/invariant merely to make review easier -- only "
                  "strengthen, clarify, or add.\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True))
        try:
            parsed = self._invoke_and_parse(provider, prompt)
        except Exception as exc:
            self.db.event("change", prior["change_id"], "SPEC_REFINE_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED" if "missing" not in str(exc).lower() else "OUTPUT_INVALID", "proposal": None, "message": str(exc)}

        stored = self._store_proposal(prior["change_id"], provider, parsed, digest, None, proposal_id, prior["refinement_round"] + 1)
        self.db.execute("UPDATE spec_proposals SET status='SUPERSEDED',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('DRAFT','REVIEWING','NEEDS_REFINEMENT')", (proposal_id,))
        return {"outcome": "READY", **stored}


# ===================================================================
# Proposal validation (E5.6) -- reuses the REAL SpecRegistry, staged
# ===================================================================
class SpecProposalValidator:
    def __init__(self, specs_root):
        self.specs_root = specs_root

    def validate(self, proposal: dict) -> dict:
        """Copies the real specs/ tree into a temp dir, overlays the
        proposed feature file, and runs the REAL SpecRegistry against
        it -- every schema/semantic rule (duplicate ids, missing
        fields, invalid status, manifest compatibility) is checked by
        the actual registry code, never a second, duplicated
        implementation of those rules."""
        from app.services.spec_registry import SpecRegistry, SpecError
        content = json.loads(proposal["proposed_content"])
        errors: list[str] = []
        warnings: list[str] = []
        if not content.get("requirements"):
            warnings.append("Proposal has no requirements at all.")
        if not content.get("acceptance_criteria"):
            errors.append("Proposal has no acceptance criteria -- every requirement needs verifiable acceptance.")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_specs = Path(tmp) / "specs"
            if Path(self.specs_root).is_dir():
                shutil.copytree(self.specs_root, tmp_specs)
            else:
                tmp_specs.mkdir(parents=True)
            features_dir = tmp_specs / "features"
            features_dir.mkdir(parents=True, exist_ok=True)
            yaml_text = _serialize_feature_yaml(content, proposal["proposed_version"], status="approved")
            # Overwrite by feature id if this feature already has a file
            # (an update), else write a new one -- matches apply_proposal's
            # own path-resolution exactly so staged validation is a
            # faithful preview of the real write.
            existing_path = None
            try:
                registry = SpecRegistry(tmp_specs).load()
                existing = registry.feature(content["feature_id"])
                if existing:
                    existing_path = Path(existing["_path"])
            except SpecError:
                pass
            target = existing_path or (features_dir / f"{content['feature_id'].lower().replace('_', '-')}.yaml")
            target.write_text(yaml_text)
            try:
                SpecRegistry(tmp_specs).load()
            except SpecError as exc:
                errors.extend(exc.errors)
        return {"valid": not errors, "errors": errors, "warnings": warnings}


# ===================================================================
# Independent Spec Review (E5.7/E5.8/E5.9) -- a fresh invocation
# ===================================================================
class SpecReviewService(_AgentRole):
    def __init__(self, db, changes, work_products, invoker, roles_catalog, specs_root, repo_root, human_decisions: HumanDecisionService):
        super().__init__(db, invoker, roles_catalog, "REVIEWER")
        self.changes = changes
        self.work_products = work_products
        self.specs_root = specs_root
        self.repo_root = repo_root
        self.human_decision_service = human_decisions

    def review(self, proposal_id: int, provider: str = "claude") -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError
        author = SpecAuthorService(self.db, self.changes, self.work_products, self.invoker, self.roles_catalog, self.specs_root, self.repo_root)
        proposal = author.get(proposal_id)
        if not proposal:
            raise SpecLifecycleError("Proposal not found")
        try:
            self._check_assignment(provider)
        except SpecLifecycleError as exc:
            self.db.event("change", proposal["change_id"], "SPEC_REVIEW_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "verdict": None, "message": str(exc)}

        change = self.changes.get(proposal["change_id"])
        related = []
        try:
            registry = SpecRegistry(self.specs_root).load()
            related = [{"id": f["id"], "title": f.get("title"), "summary": f.get("summary")}
                       for f in registry.features.values() if f.get("status") == "approved" and f["id"] != proposal["feature_id"]]
        except SpecError:
            pass
        req_wp = self.db.one("SELECT content_metadata FROM work_products WHERE kind='REQUIREMENT_ANALYSIS' AND change_id=? ORDER BY id DESC LIMIT 1", (proposal["change_id"],))
        # Deliberately NOT including any "rationale"/author-reasoning
        # field -- only the final structured proposal content itself
        # (E5's critical rule: review must not inherit hidden author
        # reasoning).
        context = {"original_intent": change.get("description") or change.get("title") if change else None,
                   "requirement_analysis": json.loads(req_wp["content_metadata"]) if req_wp else None,
                   "proposed_feature_spec": json.loads(proposal["proposed_content"]),
                   "existing_related_specs": related}
        prompt = SPEC_REVIEWER_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        try:
            raw_text = self.invoker.invoke(provider, prompt, SPEC_REVIEW_JSON_SCHEMA, self.repo_root)
            parsed = json.loads(raw_text)
        except Exception as exc:
            self.db.event("change", proposal["change_id"], "SPEC_REVIEW_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "verdict": None, "message": str(exc)}
        if not isinstance(parsed, dict) or "verdict" not in parsed:
            self.db.event("change", proposal["change_id"], "SPEC_REVIEW_OUTPUT_INVALID", "missing verdict")
            return {"outcome": "OUTPUT_INVALID", "verdict": None, "message": "Missing verdict"}

        wp_id = self.work_products.create(
            kind="SPEC_REVIEW", title=f"Spec Review: {proposal['feature_id']} v{proposal['proposed_version']}",
            project_id=proposal["project_id"], change_id=proposal["change_id"], status="DRAFT",
            content_metadata=parsed)
        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"specreview-{proposal_id}-{wp_id}", f"Spec Review: {proposal['feature_id']} v{proposal['proposed_version']}", "",
             "ACTIVE", proposal["change_id"], "SPEC_REVIEW"))
        self.work_products.link_task(task_id, wp_id, "OUTPUT")

        hd_ids = []
        if parsed.get("verdict") == "HUMAN_DECISION_REQUIRED":
            for hd in parsed.get("human_decisions") or []:
                hd_ids.append(self.human_decision_service.create(
                    "spec_proposal", proposal_id, hd.get("question") or "", hd.get("reason") or "",
                    hd.get("spec_change_signal") or "HUMAN_SPEC_CHANGE_REQUIRED"))

        self.db.execute("UPDATE spec_proposals SET review_result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(parsed), proposal_id))
        self.db.event("change", proposal["change_id"], "SPEC_REVIEWED", f"proposal={proposal_id} verdict={parsed['verdict']}")
        return {"outcome": "REVIEWED", "verdict": parsed["verdict"], "findings": parsed.get("findings") or [],
                "human_decision_ids": hd_ids, "work_product": self.work_products.get(wp_id)}


# ===================================================================
# SpecLifecycleService -- bounded refinement loop (E5.10) + apply (E5.13)
# ===================================================================
class SpecLifecycleService:
    MAX_ROUNDS = 3

    def __init__(self, db, changes, work_products, trace, requirement_analysis: RequirementAnalysisService,
                 author: SpecAuthorService, validator: SpecProposalValidator, reviewer: SpecReviewService,
                 human_decisions: HumanDecisionService, specs_root):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.requirement_analysis = requirement_analysis
        self.author = author
        self.validator = validator
        self.reviewer = reviewer
        self.human_decision_service = human_decisions
        self.specs_root = specs_root

    # ---- read ---------------------------------------------------------
    def get_proposal(self, proposal_id: int) -> dict | None:
        return self.author.get(proposal_id)

    def list_proposals(self, change_id: int) -> list[dict]:
        return self.author.list_for_change(change_id)

    def get_requirement_analysis(self, change_id: int) -> dict | None:
        return self.db.one(
            "SELECT * FROM work_products WHERE kind='REQUIREMENT_ANALYSIS' AND change_id=? ORDER BY id DESC LIMIT 1",
            (change_id,))

    def human_decisions_for(self, proposal_id: int) -> list[dict]:
        return self.human_decision_service.list_for("spec_proposal", proposal_id)

    # ---- validate + finalize (shared by author/refine paths) ---------
    def validate_proposal(self, proposal_id: int) -> dict:
        proposal = self.get_proposal(proposal_id)
        if not proposal:
            raise SpecLifecycleError("Proposal not found")
        result = self.validator.validate(proposal)
        self.db.execute("UPDATE spec_proposals SET validation_result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (json.dumps(result), proposal_id))
        if not result["valid"]:
            self.db.execute("UPDATE spec_proposals SET status='REJECTED',updated_at=CURRENT_TIMESTAMP WHERE id=?", (proposal_id,))
        return result

    def finalize_after_review(self, proposal_id: int, verdict: str) -> str:
        """Applies a review verdict to a proposal's lifecycle status.
        Never auto-applies -- READY still requires an explicit
        apply_proposal() call (E5.13)."""
        status = {"PASS": "READY", "NEEDS_REFINEMENT": "NEEDS_REFINEMENT",
                  "HUMAN_DECISION_REQUIRED": "HUMAN_DECISION_REQUIRED", "REJECT": "REJECTED"}.get(verdict, "REJECTED")
        self.db.execute("UPDATE spec_proposals SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, proposal_id))
        return status

    # ---- bounded orchestration (E5.10) --------------------------------
    def run_lifecycle(self, change_id: int, provider: str = "claude", max_rounds: int | None = None) -> dict:
        """Author -> Validate -> Review -> (Refine -> Validate -> Review)*
        up to max_rounds (default MAX_ROUNDS=3). Every individual step
        remains independently callable via its own API route -- this is
        a convenience orchestrator over exactly those same steps, never
        a separate code path. Never loops indefinitely; never weakens a
        requirement/acceptance criterion/invariant to force a PASS (that
        would require SpecAuthorService.refine's own prompt to do so,
        and its prompt explicitly forbids it)."""
        max_rounds = max_rounds if max_rounds is not None else self.MAX_ROUNDS
        ra = self.requirement_analysis.analyze(change_id, provider=provider)
        if ra["outcome"] != "READY":
            return {"outcome": ra["outcome"], "stage": "REQUIREMENT_ANALYSIS", "message": ra.get("message"), "proposal": None, "rounds": 0}

        result = self.author.author(change_id, ra["work_product"]["id"], provider=provider)
        if result["outcome"] != "READY":
            return {"outcome": result["outcome"], "stage": "SPEC_AUTHOR", "message": result.get("message"), "proposal": None, "rounds": 0}
        proposal = result["proposal"]

        for round_no in range(max_rounds):
            validation = self.validate_proposal(proposal["id"])
            if not validation["valid"]:
                return {"outcome": "PLAN_INVALID", "stage": "VALIDATE", "proposal": self.get_proposal(proposal["id"]),
                        "validation": validation, "rounds": round_no}

            self.db.execute("UPDATE spec_proposals SET status='REVIEWING',updated_at=CURRENT_TIMESTAMP WHERE id=?", (proposal["id"],))
            review = self.reviewer.review(proposal["id"], provider=provider)
            if review["outcome"] != "REVIEWED":
                return {"outcome": review["outcome"], "stage": "REVIEW", "message": review.get("message"),
                        "proposal": self.get_proposal(proposal["id"]), "rounds": round_no}

            verdict = review["verdict"]
            status = self.finalize_after_review(proposal["id"], verdict)
            if verdict == "PASS":
                return {"outcome": "READY", "stage": "REVIEW", "proposal": self.get_proposal(proposal["id"]),
                        "verdict": verdict, "rounds": round_no + 1}
            if verdict in ("HUMAN_DECISION_REQUIRED", "REJECT"):
                return {"outcome": "HUMAN_DECISION_REQUIRED" if verdict == "HUMAN_DECISION_REQUIRED" else "REJECTED",
                        "stage": "REVIEW", "proposal": self.get_proposal(proposal["id"]), "verdict": verdict, "rounds": round_no + 1}

            # NEEDS_REFINEMENT: refine into a new proposal revision and loop.
            if round_no + 1 >= max_rounds:
                return {"outcome": "NEEDS_REFINEMENT", "stage": "REVIEW", "proposal": self.get_proposal(proposal["id"]),
                        "verdict": verdict, "rounds": round_no + 1, "message": f"Refinement round limit ({max_rounds}) reached"}
            refined = self.author.refine(proposal["id"], review, provider=provider)
            if refined["outcome"] != "READY":
                return {"outcome": refined["outcome"], "stage": "REFINE", "message": refined.get("message"),
                        "proposal": self.get_proposal(proposal["id"]), "rounds": round_no + 1}
            proposal = refined["proposal"]

        return {"outcome": "NEEDS_REFINEMENT", "stage": "REVIEW", "proposal": self.get_proposal(proposal["id"]), "rounds": max_rounds}

    # ---- apply (E5.13/E5.14) -------------------------------------------
    def apply_proposal(self, proposal_id: int) -> dict:
        """The ONLY place canonical specs/**/*.yaml is written. Atomic
        (write to a temp file in the same directory, then os.replace --
        never a partial file visible mid-write), and leaves the
        approved baseline valid even on failure: the pre-write bytes
        (or the file's non-existence) are captured before writing and
        restored if post-write SpecRegistry validation fails."""
        from app.services.spec_registry import SpecRegistry, SpecError
        proposal = self.get_proposal(proposal_id)
        if not proposal:
            raise SpecLifecycleError("Proposal not found")
        if proposal["status"] != "READY":
            raise SpecLifecycleError(f"Proposal must be READY to apply (current status: {proposal['status']})")
        if any(not hd["resolved"] for hd in self.human_decisions_for(proposal_id)):
            raise SpecLifecycleError("This proposal has unresolved human decisions -- resolve them before applying")

        content = json.loads(proposal["proposed_content"])
        specs_root = Path(self.specs_root)
        features_dir = specs_root / "features"
        features_dir.mkdir(parents=True, exist_ok=True)

        existing_path = None
        try:
            registry = SpecRegistry(specs_root).load()
            existing = registry.feature(content["feature_id"])
            if existing:
                existing_path = Path(existing["_path"])
        except SpecError as exc:
            raise SpecLifecycleError(f"Cannot apply: current spec tree is already broken: {exc}") from exc

        target = existing_path or (features_dir / f"{content['feature_id'].lower().replace('_', '-')}.yaml")
        original_bytes = target.read_bytes() if target.is_file() else None
        yaml_text = _serialize_feature_yaml(content, proposal["proposed_version"], status="approved")

        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".yaml.tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(yaml_text)
            os.replace(tmp_path, target)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

        try:
            new_registry = SpecRegistry(specs_root).load()
            new_baseline = new_registry.baseline_digest()
            if new_registry.feature(content["feature_id"]) is None:
                raise SpecError([f"Applied feature {content['feature_id']} does not resolve after write"])
        except SpecError as exc:
            # Restore exactly what was there before -- the approved
            # baseline must stay valid even if this apply failed.
            if original_bytes is not None:
                target.write_bytes(original_bytes)
            else:
                target.unlink(missing_ok=True)
            raise SpecLifecycleError(f"Apply failed post-write validation, restored previous state: {exc}") from exc

        self.db.execute("UPDATE spec_proposals SET status='APPLIED',applied_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (proposal_id,))
        wp_id = self.work_products.create(
            kind="FEATURE_SPEC", title=f"{content['feature_id']} v{proposal['proposed_version']}",
            project_id=proposal["project_id"], change_id=proposal["change_id"], status="APPROVED",
            content_ref=f"spec:{content['feature_id']}@v{proposal['proposed_version']}",
            content_metadata={"proposal_id": proposal_id, "baseline_sha256": new_baseline})
        self.trace.link("change", proposal["change_id"], "spec_feature", content["feature_id"], relation="GOVERNED_BY")
        self.db.event("change", proposal["change_id"], "SPEC_PROPOSAL_APPLIED",
                       f"proposal={proposal_id} feature={content['feature_id']} v{proposal['proposed_version']} baseline={new_baseline[:12]}")
        return {"proposal_id": proposal_id, "feature_id": content["feature_id"], "version": proposal["proposed_version"],
                "baseline_sha256": new_baseline, "work_product": self.work_products.get(wp_id)}
