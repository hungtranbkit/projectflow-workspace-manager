from __future__ import annotations

"""Engineering Domain Foundation (Phase E1.5): a minimal, TYPED
source/target trace relationship -- deliberately not a giant
polymorphic system. Added only for the trace edges the E1 spec requires
that an existing column does not already express cleanly:

  Change      -> Spec (feature_id)
  WorkProduct -> Release/Deployment (deployments.id)

Every other required trace edge already has a clean, existing, typed
mechanism and is NOT duplicated here:

  Requirement -> Task        : tasks.spec_requirement_ids (Spec Layer)
  Task        -> WorkProduct : task_work_product_links (E1.4)
  Task        -> AgentSession: agent_sessions.task_id
  Task        -> Evidence    : EvidenceStore over verification_reports/
                                review_runs/qa_runs/test_runs (all
                                already task_id-keyed)

source_type/target_type are short entity names ("change", "work_product",
"spec_feature", "deployment", ...); ids are stored as text so a
trace_link can point at either a real integer row id or a stable string
id (a SpecRegistry feature_id like "FEAT-SPEC-LAYER") uniformly."""


class TraceService:
    def __init__(self, db):
        self.db = db

    def link(self, source_type: str, source_id, target_type: str, target_id, relation: str = "RELATES_TO") -> None:
        self.db.execute(
            "INSERT INTO trace_links(source_type,source_id,target_type,target_id,relation) VALUES(?,?,?,?,?) "
            "ON CONFLICT(source_type,source_id,target_type,target_id,relation) DO NOTHING",
            (source_type, str(source_id), target_type, str(target_id), relation),
        )

    def for_source(self, source_type: str, source_id) -> list[dict]:
        return self.db.all(
            "SELECT * FROM trace_links WHERE source_type=? AND source_id=? ORDER BY id",
            (source_type, str(source_id)),
        )

    def for_target(self, target_type: str, target_id) -> list[dict]:
        return self.db.all(
            "SELECT * FROM trace_links WHERE target_type=? AND target_id=? ORDER BY id",
            (target_type, str(target_id)),
        )
