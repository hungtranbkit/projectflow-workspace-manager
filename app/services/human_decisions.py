from __future__ import annotations

"""Generic Human Decision mechanism. Introduced in Phase E4 for the
Planner (as `plan_human_decisions`, Plan-only) and GENERALIZED here in
Phase E5.11 so SpecProposal reuses the exact same mechanism instead of
a second decision system -- E4's Plan, E5's SpecProposal, and a Change
directly can all create/resolve human decisions through this one
table/service. (The original `plan_human_decisions` table from V21
stays in the schema -- migrations are never rewritten after shipping --
but is superseded/unused as of V22: it held zero rows in production at
the time of this generalization, migrated forward as a no-op, and
PlannerService now reads/writes exclusively through this module.)

subject_type is one of 'change' | 'plan' | 'spec_proposal'; subject_id
is the real id of that row. A decision tied to a Plan or SpecProposal
also, transitively, belongs to that row's own Change -- pending_for_change
resolves that join explicitly rather than duplicating a change_id
column onto every decision row."""


class HumanDecisionService:
    def __init__(self, db):
        self.db = db

    def create(self, subject_type: str, subject_id: int, question: str, reason: str = "", spec_change_signal: str = "NONE") -> int:
        return self.db.execute(
            "INSERT INTO human_decisions(subject_type,subject_id,question,reason,spec_change_signal) VALUES(?,?,?,?,?)",
            (subject_type, subject_id, question, reason, spec_change_signal or "NONE"))

    def get(self, decision_id: int) -> dict | None:
        return self.db.one("SELECT * FROM human_decisions WHERE id=?", (decision_id,))

    def list_for(self, subject_type: str, subject_id: int) -> list[dict]:
        return self.db.all(
            "SELECT * FROM human_decisions WHERE subject_type=? AND subject_id=? ORDER BY id",
            (subject_type, subject_id))

    def resolve(self, decision_id: int, resolution_note: str) -> dict:
        row = self.get(decision_id)
        if not row:
            raise ValueError("Human decision not found")
        self.db.execute(
            "UPDATE human_decisions SET resolved=1,resolution_note=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            ((resolution_note or "").strip(), decision_id))
        return self.get(decision_id)

    def list_for_change(self, change_id: int) -> list[dict]:
        """Same join logic as pending_for_change() below, but EVERY row
        (pending and resolved alike) -- for the Decisions tab (E7.5.13),
        which must show both sections."""
        return self.db.all(
            "SELECT * FROM human_decisions WHERE ("
            "  (subject_type='change' AND subject_id=?)"
            "  OR (subject_type='plan' AND subject_id IN (SELECT id FROM plans WHERE change_id=?))"
            "  OR (subject_type='spec_proposal' AND subject_id IN (SELECT id FROM spec_proposals WHERE change_id=?))"
            "  OR (subject_type='work_product' AND subject_id IN (SELECT id FROM work_products WHERE change_id=?))"
            ") ORDER BY id DESC",
            (change_id, change_id, change_id, change_id))

    def list_pending_for_change(self, change_id: int) -> list[dict]:
        """Same join logic as pending_for_change() below, but the real
        rows instead of a bool -- for a Change-level overview that needs
        to say HOW MANY, not just whether any exist."""
        return self.db.all(
            "SELECT * FROM human_decisions WHERE resolved=0 AND ("
            "  (subject_type='change' AND subject_id=?)"
            "  OR (subject_type='plan' AND subject_id IN (SELECT id FROM plans WHERE change_id=?))"
            "  OR (subject_type='spec_proposal' AND subject_id IN (SELECT id FROM spec_proposals WHERE change_id=?))"
            "  OR (subject_type='work_product' AND subject_id IN (SELECT id FROM work_products WHERE change_id=?))"
            ") ORDER BY id",
            (change_id, change_id, change_id, change_id))

    def pending_for_change(self, change_id: int) -> bool:
        """True if the Change itself, any of its Plans, any of its
        SpecProposals, or any of its WorkProducts (E6.14: architecture/
        design human decisions reuse this same subject_type='work_product'
        row rather than a second decision system) has an unresolved
        human decision -- the single check WorkflowService's
        WAITING_HUMAN wiring uses (E4.12/E5.11/E6.14)."""
        row = self.db.one(
            "SELECT id FROM human_decisions WHERE resolved=0 AND ("
            "  (subject_type='change' AND subject_id=?)"
            "  OR (subject_type='plan' AND subject_id IN (SELECT id FROM plans WHERE change_id=?))"
            "  OR (subject_type='spec_proposal' AND subject_id IN (SELECT id FROM spec_proposals WHERE change_id=?))"
            "  OR (subject_type='work_product' AND subject_id IN (SELECT id FROM work_products WHERE change_id=?))"
            ") LIMIT 1",
            (change_id, change_id, change_id, change_id))
        return bool(row)
