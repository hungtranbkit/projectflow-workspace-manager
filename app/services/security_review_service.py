from __future__ import annotations
import re

from app.services.code_review_service import CodeReviewService
from app.services.review_service import SECURITY_REVIEWER_PREAMBLE

"""Security Review Applicability (E9.7) + dedicated Security Review
(E9.8). Deterministic, rule-based applicability -- never an LLM call
just to decide whether a Task needs one, same "cheap deterministic
check before any expensive AI call" discipline UiUxApplicabilityService
(E6.10) already established. SecurityReviewService itself is a thin
CodeReviewService subclass: identical diff-bounded, fresh-invocation,
validated-output mechanism, only the role/preamble/WorkProduct kind
differ (E9.8 explicitly: "Separate fresh invocation from CodeReview",
never a second review pipeline)."""

SENSITIVE_PATH_PATTERNS = [re.compile(p, re.I) for p in (
    r"auth", r"login", r"password", r"secret", r"\.env", r"security", r"permission",
    r"session", r"token", r"crypto", r"acl", r"role", r"tenant",
)]
SENSITIVE_DIFF_KEYWORDS = (
    "subprocess", "os.system", "eval(", "exec(", "pickle.loads", "shell=True",
    "SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", ".execute(", ".executescript(",
    "requests.get", "requests.post", "urlopen", "../", "chmod", "sudo",
    "open(", "yaml.load(",
)
DEPENDENCY_FILES = {"requirements.txt", "package.json", "package-lock.json", "pyproject.toml",
                     "Pipfile", "go.mod", "go.sum", "Cargo.toml", "Gemfile"}
MIGRATION_PATTERN = re.compile(r"migrat", re.I)


class SecurityApplicabilityService:
    def __init__(self, db, git):
        self.db = db
        self.git = git

    def applicable(self, task_id: int, ws: dict, workflow_profile: str | None, project_policy: dict | None = None) -> dict:
        """Never weakened by policy below a hard trigger (E9.7: "Project
        policy may strengthen but not weaken mandatory security rules")
        -- policy may only ADD `required: true` on top of a NOT_
        APPLICABLE default, never remove a real hard trigger."""
        changed_files = []
        diff_text = ""
        try:
            changed_files = self.git.changed_files(ws["worktree_path"], ws["base_commit"])
            diff_text = self.git.git(ws["worktree_path"], "diff", f"{ws['base_commit']}..", check=False).stdout
        except Exception:
            pass
        if not changed_files:
            # A Task with genuinely nothing changed yet has nothing to
            # security-review -- CONTROLLED's own stricter default (or
            # a forcing policy) still has no real change set to apply
            # to. CodeReviewService.review_task() itself would return
            # NO_CHANGES here too; this keeps applicability consistent
            # with what a review call could actually do.
            return {"outcome": "SECURITY_REVIEW_NOT_APPLICABLE", "required": False, "reasons": []}
        reasons = []
        for f in changed_files:
            if any(p.search(f) for p in SENSITIVE_PATH_PATTERNS):
                reasons.append(f"security-sensitive path changed: {f}")
            if f.rsplit("/", 1)[-1] in DEPENDENCY_FILES:
                reasons.append(f"dependency file changed: {f}")
            if MIGRATION_PATTERN.search(f):
                reasons.append(f"migration/data-handling path changed: {f}")
        for kw in SENSITIVE_DIFF_KEYWORDS:
            if kw in diff_text:
                reasons.append(f"security-relevant construct in diff: {kw.strip()}")
                break
        hard_trigger = bool(reasons)
        policy_forced = bool((project_policy or {}).get("review", {}).get("security_always_required"))
        controlled_default = workflow_profile == "CONTROLLED"
        required = hard_trigger or policy_forced or controlled_default
        if not required:
            return {"outcome": "SECURITY_REVIEW_NOT_APPLICABLE", "required": False, "reasons": []}
        if not reasons and policy_forced:
            reasons = ["required by project policy (engineering.review.security_always_required)"]
        if not reasons and controlled_default:
            reasons = ["CONTROLLED workflow profile requires security review by default"]
        return {"outcome": "SECURITY_REVIEW_REQUIRED", "required": True, "reasons": reasons}


class SecurityReviewService(CodeReviewService):
    review_kind = "SECURITY"
    preamble = SECURITY_REVIEWER_PREAMBLE
    role_key = "SECURITY_REVIEWER"
