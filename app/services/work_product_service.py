from __future__ import annotations
import json

"""Engineering Domain Foundation (Phase E1.3/E1.4): WorkProduct is a
durable engineering output -- a generic, typed-kind core representation
(never one table per kind, per the E1 spec). Content itself is never
stored inline on the row: `content_ref` points at where the real
content lives (a spec file path, a verification_reports.id, a free-form
URI, ...) and `content_metadata` carries small structured facts --
same "reference/index row, never a second copy of real content"
discipline the Spec Layer already established (see spec_registry.py).

History-friendly (E1.3): a revision is a NEW row with `supersedes_id`
pointing at the one it replaces; `create(..., supersedes_id=...)` marks
the old row SUPERSEDED but never mutates its title/content/status
fields -- no historical engineering decision is ever silently
overwritten."""

WORK_PRODUCT_KINDS = (
    "REQUIREMENT_ANALYSIS", "FEATURE_SPEC", "SPEC_REVIEW", "ARCHITECTURE_ANALYSIS", "ADR",
    "TECHNICAL_DESIGN", "UI_UX_DESIGN", "TEST_PLAN", "TEST_CASE_SET", "CODE_CHANGE",
    "REVIEW_REPORT", "VERIFICATION_REPORT", "RELEASE_MANIFEST", "DEPLOYMENT_RECORD",
    "INCIDENT_REPORT", "HUMAN_DECISION",
)
WORK_PRODUCT_STATUSES = ("DRAFT", "PROPOSED", "APPROVED", "SUPERSEDED", "REJECTED")
DIRECTIONS = ("INPUT", "OUTPUT")


class WorkProductError(ValueError):
    pass


class WorkProductService:
    def __init__(self, db):
        self.db = db

    def create(self, *, kind: str, title: str, project_id: int | None = None, change_id: int | None = None,
               task_id: int | None = None, status: str = "DRAFT", content_ref: str | None = None,
               content_metadata: dict | None = None, content_digest: str | None = None,
               supersedes_id: int | None = None) -> int:
        kind = (kind or "").strip().upper()
        if kind not in WORK_PRODUCT_KINDS:
            raise WorkProductError(f"Unknown work product kind: {kind} (must be one of {WORK_PRODUCT_KINDS})")
        title = (title or "").strip()
        if not title:
            raise WorkProductError("WorkProduct title is required")
        status = (status or "DRAFT").strip().upper()
        if status not in WORK_PRODUCT_STATUSES:
            raise WorkProductError(f"Unknown status: {status} (must be one of {WORK_PRODUCT_STATUSES})")
        if project_id is not None and not self.db.one("SELECT id FROM repositories WHERE id=?", (project_id,)):
            raise WorkProductError(f"Unknown project_id: {project_id}")
        if change_id is not None and not self.db.one("SELECT id FROM changes WHERE id=?", (change_id,)):
            raise WorkProductError(f"Unknown change_id: {change_id}")
        if task_id is not None and not self.db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
            raise WorkProductError(f"Unknown task_id: {task_id}")
        if supersedes_id is not None:
            prev = self.get(supersedes_id)
            if not prev:
                raise WorkProductError("supersedes_id does not reference a real WorkProduct")
            self.db.execute(
                "UPDATE work_products SET status='SUPERSEDED',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (supersedes_id,),
            )
        wpid = self.db.execute(
            "INSERT INTO work_products(project_id,change_id,task_id,kind,title,status,content_ref,content_metadata,content_digest,supersedes_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (project_id, change_id, task_id, kind, title, status, content_ref,
             json.dumps(content_metadata or {}), content_digest, supersedes_id),
        )
        self.db.event("work_product", wpid, "WORK_PRODUCT_CREATED", f"kind={kind}" + (f" supersedes={supersedes_id}" if supersedes_id else ""))
        return wpid

    def get(self, wp_id: int) -> dict | None:
        return self.db.one("SELECT * FROM work_products WHERE id=?", (wp_id,))

    def list_for_change(self, change_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM work_products WHERE change_id=? ORDER BY id", (change_id,))

    def list_for_task(self, task_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM work_products WHERE task_id=? ORDER BY id", (task_id,))

    # ---- Task input/output contracts (E1.4) --------------------------
    def link_task(self, task_id: int, work_product_id: int, direction: str) -> None:
        """Task -> WorkProduct as an INPUT or OUTPUT reference. Never
        stores the WorkProduct's own content on the Task row -- this is
        a relationship, not a copy."""
        direction = (direction or "").strip().upper()
        if direction not in DIRECTIONS:
            raise WorkProductError(f"direction must be one of {DIRECTIONS}")
        if not self.db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
            raise WorkProductError("Task not found")
        if not self.get(work_product_id):
            raise WorkProductError("WorkProduct not found")
        self.db.execute(
            "INSERT INTO task_work_product_links(task_id,work_product_id,direction) VALUES(?,?,?) "
            "ON CONFLICT(task_id,work_product_id,direction) DO NOTHING",
            (task_id, work_product_id, direction),
        )
        self.db.event("task", task_id, "WORK_PRODUCT_LINKED", f"work_product={work_product_id} direction={direction}")

    def inputs_for_task(self, task_id: int) -> list[dict]:
        return self.db.all(
            "SELECT wp.* FROM work_products wp JOIN task_work_product_links l ON l.work_product_id=wp.id "
            "WHERE l.task_id=? AND l.direction='INPUT' ORDER BY wp.id",
            (task_id,),
        )

    def outputs_for_task(self, task_id: int) -> list[dict]:
        return self.db.all(
            "SELECT wp.* FROM work_products wp JOIN task_work_product_links l ON l.work_product_id=wp.id "
            "WHERE l.task_id=? AND l.direction='OUTPUT' ORDER BY wp.id",
            (task_id,),
        )
