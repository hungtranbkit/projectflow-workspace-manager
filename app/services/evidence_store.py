from __future__ import annotations

"""Spec Layer V1 (S8): EvidenceStore is a QUERY LAYER over ProjectFlow's
existing evidence tables -- verification_reports, review_runs, qa_runs,
test_runs, manual_verifications -- never a new, parallel evidence table.
Every record this returns already lives in one of those tables (with the
same hashing/integrity behavior those tables already have -- commit_sha/
reviewed_commit/tested_commit pinning is untouched by this file); this
only assembles a unified, spec-traceable view across them so Spec ->
Task -> Agent execution -> Verification -> Evidence is one real query
path, not four separate ones a caller has to know to run itself."""


class EvidenceStore:
    def __init__(self, db):
        self.db = db

    def for_task(self, task_id: int) -> dict:
        """Every piece of real evidence ProjectFlow already recorded for
        one Task, grouped by kind. Read-only; this never writes."""
        reports = self.db.all(
            "SELECT * FROM verification_reports WHERE task_id=? ORDER BY id", (task_id,)
        )
        reviews = self.db.all(
            "SELECT r.* FROM review_runs r WHERE r.task_id=? ORDER BY r.id", (task_id,)
        )
        qa_runs = self.db.all(
            "SELECT * FROM qa_runs WHERE task_id=? ORDER BY id", (task_id,)
        )
        test_runs = self.db.all(
            "SELECT tr.* FROM test_runs tr JOIN agent_workspaces w ON w.id=tr.workspace_id "
            "WHERE tr.workspace_type='agent' AND w.task_id=? ORDER BY tr.id",
            (task_id,),
        )
        manual = self.db.all(
            "SELECT mv.* FROM manual_verifications mv WHERE mv.task_id=? ORDER BY mv.id",
            (task_id,),
        )
        return {
            "verification_reports": reports,
            "review_runs": reviews,
            "qa_runs": qa_runs,
            "test_runs": test_runs,
            "manual_verifications": manual,
        }

    def spec_traced_reports(self, feature_id: str) -> list[dict]:
        """Every verification_reports row ever stamped with this exact
        feature_id -- the reverse direction of the same trace (Evidence
        -> which Task/spec it was produced for), e.g. for an eventual
        Impact Analyzer (S11) asking "what evidence exists for this
        feature across every Task that ever touched it"."""
        return self.db.all(
            "SELECT * FROM verification_reports WHERE spec_feature_id=? ORDER BY id",
            (feature_id,),
        )
