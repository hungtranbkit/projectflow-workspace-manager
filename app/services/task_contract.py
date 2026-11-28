"""Deterministic validation for the human-authored coding Task contract.

This module is deliberately pure: it reads only the Task fields supplied
by its caller and never writes evidence, creates workspaces, or starts an
executor. The seven section names are the public V2 contract.
"""
from __future__ import annotations

import re


REQUIRED_SECTIONS = (
    "OBJECTIVE", "BASELINE", "SCOPE", "EXECUTION_PLAN",
    "CONSTRAINTS", "ACCEPTANCE_DOD", "EVIDENCE",
)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+([A-Z][A-Z0-9_]+)\s*#*\s*$", re.I)
_ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S.*$")
_PLACEHOLDER = re.compile(
    r"\b(?:tbd|todo|tbc|to be determined|fill\s*me|placeholder|insert here|coming soon)\b|…|\.{3,}", re.I
)
_GENERIC_LINE = re.compile(
    r"^(?:ok(?:ay)?|done|same as above|as requested|implement as needed|fix the issue|"
    r"follow best practices|do the needful|n/?a|none|tbd|todo)[.! ]*$", re.I
)
_COMMAND = re.compile(
    r"(?:^|\s)(?:pytest\b|python(?:3)?\s+-m\s+pytest\b|python(?:3)?\s+[-\w./]+|"
    r"curl\s+\S+|git\s+(?:status|diff|rev-parse|log|show)\b|"
    r"(?:\./)?scripts/(?:preflight|test|build|smoke)\.sh\b|"
    r"npm\s+(?:run\s+)?(?:test|build|lint)\b|playwright\s+test\b)", re.I | re.M
)
_PATH = re.compile(r"(?:^|[\s`'\"])(?:[\w.-]+/)+[\w.-]+(?:\.[A-Za-z0-9_-]+)?(?:$|[\s`'\"])")
_ACTION = re.compile(r"\b(?:add|change|fix|implement|remove|update|preserve|verify|record|test|run|reject|return|show|display|create|prevent)\b", re.I)
_OUTCOME = re.compile(r"\b(?:returns?|shows?|displays?|rejects?|accepts?|contains?|prevents?|exactly|at most|at least|pass(?:es)?|fail(?:s)?|exit(?:s)?\s*(?:code)?\s*0|status\s*=?\s*\d+)\b|(?:==|!=|<=|>=)", re.I)
_ROLLBACK = re.compile(r"\b(?:rollback|revert|restore|recover)\b", re.I)
_DEPENDENCY = re.compile(r"\b(?:dependenc(?:y|ies)|depends? on|requires?|prerequisite)\b", re.I)
_TEST = re.compile(r"\b(?:test|pytest|unit|integration|e2e|smoke|security|qa)\b", re.I)
_EXPECTED = re.compile(r"\b(?:expected|must|returns?|shows?|rejects?|accepts?|pass(?:es)?|fail(?:s)?|exit(?:s)?\s*(?:code)?\s*0|status\s*=?\s*\d+)\b|(?:==|!=|<=|>=)", re.I)
_PROOF = re.compile(r"\b(?:evidence|proof|output|result|log|commit|sha256|report|record|artifact|url|status)\b", re.I)


def _words(value: str) -> set[str]:
    return {w.casefold() for w in re.findall(r"[\w-]+", value, re.UNICODE) if len(w) > 2}


def _section_issue(name: str, body: str, title: str) -> str | None:
    text = body.strip()
    if not text or all(not line.strip() or _GENERIC_LINE.fullmatch(line.strip()) for line in text.splitlines()):
        return "EMPTY_OR_GENERIC"
    if _PLACEHOLDER.search(text):
        return "PLACEHOLDER_CONTENT"
    if len(_words(text)) < 6:
        return "INSUFFICIENT_DETAIL"

    if name == "OBJECTIVE":
        task_words = _words(title)
        if not title or not task_words or not task_words.issubset(_words(text)):
            return "NOT_TASK_SPECIFIC"
        if not _ACTION.search(text) or not _OUTCOME.search(text):
            return "MISSING_ACTION_OR_OBSERVABLE_OUTCOME"
    elif name == "BASELINE":
        if not _PATH.search(text):
            return "MISSING_EXPLICIT_PATH"
        if not re.search(r"\b(?:branch|sha|commit|version|currently|observed|baseline)\b", text, re.I):
            return "MISSING_BASELINE_REFERENCE"
    elif name == "SCOPE":
        if not _PATH.search(text):
            return "MISSING_EXPLICIT_PATH"
        if not re.search(r"\b(?:in scope|included|change)\b", text, re.I):
            return "MISSING_IN_SCOPE_BOUNDARY"
        if not re.search(r"\b(?:out of scope|excluded|do not change|must not change)\b", text, re.I):
            return "MISSING_OUT_OF_SCOPE_BOUNDARY"
    elif name == "EXECUTION_PLAN":
        numbered = re.findall(r"(?m)^\s*(\d+)[.)]\s+(.+)$", text)
        if len(numbered) < 3 or [int(n) for n, _ in numbered] != list(range(1, len(numbered) + 1)):
            return "MISSING_ORDERED_STEPS"
        if not _COMMAND.search(text.replace("`", "")):
            return "MISSING_EXACT_COMMANDS"
    elif name == "CONSTRAINTS":
        if not _DEPENDENCY.search(text):
            return "MISSING_DEPENDENCIES"
        if not _ROLLBACK.search(text):
            return "MISSING_ROLLBACK"
        if not re.search(r"\b(?:security|secret|permission|production|boundary|risk|scope)\b", text, re.I):
            return "MISSING_BOUNDARY_RISK"
    elif name == "ACCEPTANCE_DOD":
        if not _TEST.search(text) or not _EXPECTED.search(text):
            return "MISSING_TEST_OR_EXPECTED_RESULT"
        if not _COMMAND.search(text.replace("`", "")):
            return "MISSING_TEST_COMMAND"
    elif name == "EVIDENCE":
        if not _PROOF.search(text) or not _PATH.search(text) and not re.search(r"\b(?:sha256|commit|http|https|test output)\b", text, re.I):
            return "MISSING_VERIFIABLE_PROOF"
    return None


def validate_task_contract(task: dict) -> dict:
    """Validate the exact seven-section contract and return actionable data."""
    source = str(task.get("implementation_prompt") or "")
    sections: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    current: str | None = None
    outside_content = False
    unsupported_headings: list[str] = []
    for line in source.splitlines():
        match = _HEADING.match(line)
        heading = match.group(1).upper() if match else None
        if heading in REQUIRED_SECTIONS:
            counts[heading] = counts.get(heading, 0) + 1
            sections.setdefault(heading, [])
            current = heading
        elif _ANY_HEADING.match(line):
            unsupported_headings.append(line.strip())
            current = None
        elif current:
            sections[current].append(line)
        elif line.strip():
            outside_content = True

    missing: list[str] = []
    issues: dict[str, str] = {}
    bodies = {key: "\n".join(value).strip() for key, value in sections.items()}
    for name in REQUIRED_SECTIONS:
        if counts.get(name, 0) == 0:
            missing.append(name)
            issues[name] = "MISSING"
        elif counts[name] > 1:
            missing.append(name)
            issues[name] = "DUPLICATE_SECTION"
        else:
            issue = _section_issue(name, bodies.get(name, ""), str(task.get("title") or "").strip())
            if issue:
                missing.append(name)
                issues[name] = issue

    contract_issues = []
    if outside_content:
        contract_issues.append("CONTENT_OUTSIDE_REQUIRED_SECTIONS")
    if unsupported_headings:
        contract_issues.append("UNSUPPORTED_OR_GENERIC_HEADING")

    return {
        "ready": not missing and not contract_issues,
        "contract_version": "V2",
        "source": "IMPLEMENTATION_PROMPT",
        "required_sections": list(REQUIRED_SECTIONS),
        "missing_sections": missing,
        "section_issues": issues,
        "contract_issues": contract_issues,
        "reason_code": None if not missing else "TASK_CONTRACT_INCOMPLETE",
    }
