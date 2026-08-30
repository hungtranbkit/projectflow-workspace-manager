from __future__ import annotations
import hashlib
import json
import re

"""Independent Code Review, Security Review & Autonomous Fix Loop
(Phase E9). Discovery: review_runs already IS a durable, commit-pinned
review record (task_id/workspace_id/reviewer_agent/reviewed_commit/
status/findings) used by the existing manual Submit-for-Review flow --
extended additively (see app/db.py's V26 migration comment) for E9's
own AI-driven, structured, commit/baseline-bound reviews, never a
second review table. `findings` is the one genuinely new table --
review_runs.findings is a flat TEXT blob, never queryable per-finding.

CRITICAL independence rule (E9's own PRODUCT PRINCIPLE): every review
call below is a brand-new PlannerAgentInvoker.invoke() subprocess --
`claude -p ... --tools "" --max-turns 1` (the exact mechanism E4-E7's
own reviewers already use). Tools are disabled and there is zero
session continuity, so a reviewer LITERALLY CANNOT see anything the
Builder's own interactive session thought/said -- only what THIS
module puts in its own prompt text (the real diff + governing
context), by construction, not by convention."""

FINDING_CATEGORIES = (
    "CORRECTNESS", "SPEC_COMPLIANCE", "DESIGN_COMPLIANCE", "TEST_COVERAGE",
    "ERROR_HANDLING", "DATA_INTEGRITY", "CONCURRENCY", "PERFORMANCE",
    "SECURITY", "MAINTAINABILITY", "SCOPE", "OBSERVABILITY",
)
FINDING_SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
BLOCKING_SEVERITIES = ("HIGH", "CRITICAL")
REVIEW_VERDICTS = ("PASS", "PASS_WITH_FINDINGS", "FIX_REQUIRED", "HUMAN_DECISION_REQUIRED", "REJECT")
FINDING_STATUSES = ("OPEN", "RESOLVED", "WONT_FIX", "SUPERSEDED")

FINDING_ITEM_SCHEMA = {
    "type": "object",
    "required": ["category", "severity", "title", "description"],
    "properties": {
        "category": {"type": "string", "enum": list(FINDING_CATEGORIES)},
        "severity": {"type": "string", "enum": list(FINDING_SEVERITIES)},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "file_path": {"type": ["string", "null"]},
        "line_start": {"type": ["integer", "null"]},
        "line_end": {"type": ["integer", "null"]},
        "requirement_ids": {"type": "array", "items": {"type": "string"}},
        "acceptance_ids": {"type": "array", "items": {"type": "string"}},
        "invariant_ids": {"type": "array", "items": {"type": "string"}},
        "test_case_ids": {"type": "array", "items": {"type": "string"}},
    },
}
REVIEW_JSON_SCHEMA = {
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": list(REVIEW_VERDICTS)},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": FINDING_ITEM_SCHEMA},
        "human_decisions": {
            "type": "array",
            "items": {"type": "object", "required": ["question"],
                      "properties": {"question": {"type": "string"}, "reason": {"type": "string"}}},
        },
    },
}

CODE_REVIEWER_PREAMBLE = """You are an INDEPENDENT code reviewer. You did not write this code and \
have no access to the Builder's own reasoning, chat, or hidden notes -- only the governing Spec/ \
Architecture/Design/Test context and the actual diff below.

A test passing is NOT sufficient evidence the implementation is correct. Judge the actual diff \
against the actual requirements/acceptance criteria/invariants.

Explicitly assess: SPEC COMPLIANCE, DESIGN COMPLIANCE, CORRECTNESS (edge cases, state transitions, \
data logic), FAILURE HANDLING (error paths, retries/idempotency, cleanup), TEST CONTRACT (relevant \
TestCaseSpecs implemented/mapped, no failing test deleted/disabled, no assertion weakened), SCOPE \
(no unjustified out-of-scope change), MAINTAINABILITY, and SECURITY SIGNALS (auth, authorization, \
secret handling, injection, unsafe deserialization, path/file access, command execution) even \
though a dedicated Security Review may run separately.

Return ONLY the requested JSON. verdict PASS requires zero HIGH/CRITICAL findings. \
HUMAN_DECISION_REQUIRED requires a real question about WHAT the product should do -- never invent \
new product behavior yourself. Do not edit anything; you only report findings."""

SECURITY_REVIEWER_PREAMBLE = """You are an INDEPENDENT security reviewer. You did not write this \
code and have no access to the Builder's own reasoning or chat -- only the governing Spec/Design \
context and the actual diff below.

Assess: authentication, authorization, access boundaries, tenant isolation, injection (SQL, \
command, path traversal), XSS/CSRF/SSRF where applicable, unsafe deserialization, unsafe file \
upload/access, secret handling, insecure defaults, dependency risk signals, destructive data \
behavior, sensitive logging, privilege escalation, and any violated security invariant.

Return ONLY the requested JSON. Set every finding's category to SECURITY unless a non-security \
issue is directly security-relevant. A CRITICAL finding means autonomous progression must stop --// \
be precise about severity, never inflate or deflate it. Do not edit anything; you only report \
findings."""


class ReviewError(ValueError):
    pass


# ===================================================================
# Findings (E9.2/E9.11)
# ===================================================================
def _fingerprint(category: str, file_path: str | None, title: str) -> str:
    normalized = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha256(f"{category}|{file_path or ''}|{normalized}".encode()).hexdigest()[:24]


def task_chain_ids(db, task_id: int) -> list[int]:
    """Walks a Fix Task's ancestry (tasks.fix_of_task_id) back to the
    original Task it repairs. Ownership of a worktree transfers to a
    Fix Task (E9.14's own documented, real UNIQUE-constraint reason),
    but Findings/reviews created under the EARLIER task_id in the same
    repair chain must stay visible/resolvable once that transfer
    happens -- every chain-aware lookup below scopes to the WHOLE
    chain, never a single task_id in isolation (a real bug caught by
    this phase's own real Fix-loop test: an original Task's finding
    was orphaned and never resolved once its Fix Task took over)."""
    ids = [task_id]
    current = task_id
    while True:
        row = db.one("SELECT fix_of_task_id FROM tasks WHERE id=?", (current,))
        if not row or not row["fix_of_task_id"]:
            break
        current = row["fix_of_task_id"]
        ids.append(current)
    return ids


class FindingsStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _ids(task_id) -> list[int]:
        return list(task_id) if isinstance(task_id, (list, tuple, set)) else [task_id]

    def create_or_dedupe(self, *, change_id, task_id, review_id, category, severity, title,
                          description="", file_path=None, line_start=None, line_end=None,
                          requirement_ids=None, acceptance_ids=None, invariant_ids=None, test_case_ids=None,
                          dedupe_task_ids=None) -> int:
        """A repeated re-review round commonly re-surfaces the SAME
        unresolved issue -- deduped by a stable fingerprint (category +
        file_path + normalized title) scoped to `dedupe_task_ids`
        (defaults to just `task_id`; a caller mid-fix-chain should pass
        the FULL chain from task_chain_ids() so a re-raised issue still
        dedupes against its own history), never a fresh duplicate row
        per round (E9.11). The new row itself always attaches to the
        single `task_id` given (the CURRENT governing Task) -- only the
        dedup lookup is chain-wide. An existing OPEN finding with the
        same fingerprint is left exactly as-is (its own created_at/id
        stay the original); only a genuinely new fingerprint creates a
        new row."""
        fp = _fingerprint(category, file_path, title)
        scope = self._ids(dedupe_task_ids) if dedupe_task_ids is not None else [task_id]
        existing = self.db.one(
            "SELECT id FROM findings WHERE task_id IN (%s) AND fingerprint=? AND status='OPEN'" % ",".join("?" * len(scope)),
            (*scope, fp))
        if existing:
            return existing["id"]
        return self.db.execute(
            "INSERT INTO findings(change_id,task_id,review_id,category,severity,title,description,file_path,"
            "line_start,line_end,requirement_ids,acceptance_ids,invariant_ids,test_case_ids,fingerprint,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')",
            (change_id, task_id, review_id, category, severity, title, description, file_path, line_start, line_end,
             json.dumps(requirement_ids or []), json.dumps(acceptance_ids or []),
             json.dumps(invariant_ids or []), json.dumps(test_case_ids or []), fp))

    def list_for_task(self, task_id, status: str | None = None) -> list[dict]:
        """`task_id` may be a single int or a list/tuple of ids (pass
        task_chain_ids() for a Fix Task so its ORIGINAL Task's own
        findings remain visible)."""
        ids = self._ids(task_id)
        placeholders = ",".join("?" * len(ids))
        if status:
            return self.db.all(f"SELECT * FROM findings WHERE task_id IN ({placeholders}) AND status=? ORDER BY id", (*ids, status))
        return self.db.all(f"SELECT * FROM findings WHERE task_id IN ({placeholders}) ORDER BY id", tuple(ids))

    def open_blocking(self, task_id) -> list[dict]:
        return [f for f in self.list_for_task(task_id, "OPEN") if f["severity"] in BLOCKING_SEVERITIES]

    def resolve(self, finding_id: int, resolution_reference: str, status: str = "RESOLVED") -> dict:
        """Never closed just because a later reviewer omitted it (E9.11)
        -- this is the ONLY path a finding leaves OPEN, and it always
        requires an explicit resolution_reference (a commit sha, a
        re-review id, or an operator's own note)."""
        if status not in FINDING_STATUSES:
            raise ReviewError(f"Unknown finding status: {status}")
        if not self.db.one("SELECT id FROM findings WHERE id=?", (finding_id,)):
            raise ReviewError("Finding not found")
        if not (resolution_reference or "").strip():
            raise ReviewError("resolution_reference is required to resolve a finding")
        self.db.execute("UPDATE findings SET status=?,resolution_reference=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                         (status, resolution_reference.strip(), finding_id))
        self.db.event("finding", finding_id, "FINDING_RESOLVED", f"status={status} ref={resolution_reference}")
        return self.db.one("SELECT * FROM findings WHERE id=?", (finding_id,))

    def auto_resolve_stale(self, task_id, new_head_commit: str, note: str) -> None:
        """A Fix Builder's new commit supersedes every finding that was
        open against the PREVIOUS head -- resolved with an explicit
        evidence link (the new commit), never silently. Re-review then
        either confirms the issue is gone or re-raises it fresh (same
        fingerprint, new OPEN row via create_or_dedupe)."""
        for f in self.list_for_task(task_id, "OPEN"):
            self.resolve(f["id"], f"superseded by {new_head_commit}: {note}", status="SUPERSEDED")


# ===================================================================
# Review result validation (E9.10) -- never trust reviewer JSON blindly
# ===================================================================
class ReviewResultValidator:
    def validate(self, parsed: dict, *, changed_files: list[str], known_requirement_ids: set[str],
                 known_acceptance_ids: set[str], known_invariant_ids: set[str], known_test_case_ids: set[str]) -> list[str]:
        """Returns a list of problems (empty = valid). Never raises --
        the caller decides REVIEW_OUTPUT_INVALID vs proceeding."""
        problems: list[str] = []
        if not isinstance(parsed, dict):
            return ["not a JSON object"]
        verdict = parsed.get("verdict")
        if verdict not in REVIEW_VERDICTS:
            problems.append(f"invalid verdict: {verdict!r}")
        findings = parsed.get("findings")
        if not isinstance(findings, list):
            problems.append("findings must be a list")
            findings = []
        blocking = False
        for f in findings:
            if not isinstance(f, dict):
                problems.append("finding is not an object")
                continue
            if f.get("category") not in FINDING_CATEGORIES:
                problems.append(f"invalid finding category: {f.get('category')!r}")
            if f.get("severity") not in FINDING_SEVERITIES:
                problems.append(f"invalid finding severity: {f.get('severity')!r}")
            elif f.get("severity") in BLOCKING_SEVERITIES:
                blocking = True
            fp = f.get("file_path")
            if fp and changed_files and fp not in changed_files:
                problems.append(f"finding references a file not in this change set: {fp!r}")
            for key, known in (("requirement_ids", known_requirement_ids), ("acceptance_ids", known_acceptance_ids),
                               ("invariant_ids", known_invariant_ids), ("test_case_ids", known_test_case_ids)):
                for ref in f.get(key) or []:
                    if known and ref not in known:
                        problems.append(f"finding references unknown {key[:-1]}: {ref!r}")
        # E9.3's own consistency rule: PASS cannot coexist with a
        # blocking (HIGH/CRITICAL) finding.
        if verdict == "PASS" and blocking:
            problems.append("verdict PASS is invalid alongside a HIGH/CRITICAL finding")
        if verdict == "HUMAN_DECISION_REQUIRED":
            hds = parsed.get("human_decisions")
            if not isinstance(hds, list) or not hds or not any((hd or {}).get("question") for hd in hds):
                problems.append("HUMAN_DECISION_REQUIRED requires at least one real question")
        return problems


# ===================================================================
# Shared plumbing (mirrors _AgentRole in spec_lifecycle_service.py)
# ===================================================================
class _ReviewerRole:
    def __init__(self, db, invoker, roles_catalog, role_key):
        self.db = db
        self.invoker = invoker
        self.roles_catalog = roles_catalog
        self.role_key = role_key

    def _check_assignment(self, provider, project_policy=None):
        assignment = self.roles_catalog.validate_assignment(provider, self.role_key, project_policy)
        if not assignment["valid"]:
            raise ReviewError(f"Provider '{provider}' cannot act as {self.role_key}: missing {assignment['missing_required_capabilities']}")


# ===================================================================
# Diff bounding (E9.5) -- real diff, chunked deterministically if large
# ===================================================================
DIFF_CHUNK_BUDGET = 30000


def bounded_diffs(git, worktree_path, base_commit, head_commit, changed_files: list[str]) -> tuple[list[dict], bool]:
    """Returns (chunks, complete). Each chunk is {"files": [...], "diff": "..."}.
    A single chunk covering everything when it fits the budget; one
    chunk per file (never silently dropped) when it doesn't. `complete`
    is False only if a per-file diff for some file could not be
    produced at all (a real git error) -- that file's absence must be
    surfaced as REVIEW_CONTEXT_INCOMPLETE, never silently treated as
    reviewed."""
    full = git.git(worktree_path, "diff", f"{base_commit}..{head_commit}", check=False).stdout
    if len(full) <= DIFF_CHUNK_BUDGET:
        return [{"files": changed_files, "diff": full}], True
    chunks = []
    complete = True
    for f in changed_files:
        try:
            d = git.git(worktree_path, "diff", f"{base_commit}..{head_commit}", "--", f, check=False).stdout
            chunks.append({"files": [f], "diff": d[:DIFF_CHUNK_BUDGET]})
        except Exception:
            complete = False
    return chunks, complete


VERDICT_PRECEDENCE = {"REJECT": 4, "HUMAN_DECISION_REQUIRED": 3, "FIX_REQUIRED": 2, "PASS_WITH_FINDINGS": 1, "PASS": 0}


def aggregate_verdict(verdicts: list[str]) -> str:
    if not verdicts:
        return "PASS"
    return max(verdicts, key=lambda v: VERDICT_PRECEDENCE.get(v, 0))
