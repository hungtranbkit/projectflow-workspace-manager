#!/usr/bin/env python3
"""Run opt-in task-specific quality checks chosen by a committed policy.

No merge/deploy/CI actions. This pilot is not a production approval signal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from agent_quality_pilot import REPO_ROOT, TASK_RE, quality_gate, safe_repo_file

POLICY_RELATIVE_PATH = "config/agent_quality_tasks_pilot.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_task_policy(task_id: str, root: Path = REPO_ROOT) -> tuple[dict, Path]:
    """Resolve the task's lint scope, test suite, and UI requirement without CLI overrides."""
    if not TASK_RE.fullmatch(task_id):
        raise ValueError("Invalid task ID")
    root = root.resolve()
    policy_path = (root / POLICY_RELATIVE_PATH).resolve()
    if not policy_path.is_relative_to(root) or not policy_path.is_file():
        raise ValueError("Trusted task policy file missing")
    doc = json.loads(policy_path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1 or not isinstance(doc.get("tasks"), dict):
        raise ValueError("Unsupported task policy schema")
    task = doc["tasks"].get(task_id)
    if not isinstance(task, dict):
        raise ValueError(f"Task is not authorized by the pilot policy: {task_id}")
    source_files = task.get("code_files")
    test_files = task.get("test_files")
    ui_required = task.get("ui_required")
    if (
        not isinstance(source_files, list) or not source_files or
        not all(isinstance(p, str) and p for p in source_files) or
        len(set(source_files)) != len(source_files) or
        not isinstance(test_files, list) or not test_files or
        not all(isinstance(p, str) and p for p in test_files) or
        len(set(test_files)) != len(test_files) or
        type(ui_required) is not bool
    ):
        raise ValueError("Incomplete or invalid policy scope")
    for filename in source_files:
        safe_repo_file(filename, root)
    tests_root = (root / "tests").resolve()
    for filename in test_files:
        path = safe_repo_file(filename, root)
        if not path.is_relative_to(tests_root) or not path.name.startswith("test_"):
            raise ValueError(f"Policy test is not an approved tests/test_*.py file: {filename}")
    return {"code_files": source_files, "test_files": test_files, "ui_required": ui_required}, policy_path


def run_task_gate(task_id: str, *, root: Path = REPO_ROOT,
                  report_dir: Path | None = None, ui_evidence: str | None = None) -> dict:
    """Use only the tests pinned in a reviewed, version-controlled policy."""
    root = root.resolve()
    task, policy_path = load_task_policy(task_id, root)
    command = [sys.executable, "-m", "pytest", "-q", *task["test_files"]]
    report = quality_gate(task_id, task["code_files"], root=root,
                          report_dir=report_dir, require_ui=task["ui_required"],
                          ui_evidence=ui_evidence, test_command=command)
    tracked_paths = [*task["code_files"], *task["test_files"], POLICY_RELATIVE_PATH]
    try:
        git_status = subprocess.run(
            ["git", "status", "--porcelain", "--", *tracked_paths],
            cwd=root, capture_output=True, text=True, check=False, timeout=5
        )
        dirty = bool(git_status.stdout.strip()) if git_status.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        dirty = None
    report.update({
        "policy_file": POLICY_RELATIVE_PATH,
        "policy_sha256": _digest(policy_path),
        "code_sha256": {name: _digest(safe_repo_file(name, root)) for name in task["code_files"]},
        "test_sha256": {name: _digest(safe_repo_file(name, root)) for name in task["test_files"]},
        "scope_dirty": dirty,
        "policy_locked": True,
        "runtime_scope": "PINNED_PILOT_TASK_TESTS_ONLY",
        "limitations": (
            "PASS means lint and the task's pinned automated tests passed on this checkout. "
            "It does NOT establish visual acceptance, correctness of all features, "
            "independent review, merge readiness or deployed success."
        ),
    })
    Path(report["report_file"]).write_text(
        json.dumps({key: value for key, value in report.items() if key != "report_file"},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--ui-evidence", help="Existing UI screenshot/video under checkout")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    try:
        report = run_task_gate(args.task_id, report_dir=args.report_dir,
                               ui_evidence=args.ui_evidence)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": report["status"], "task_id": report["task_id"],
                      "tests": report["test_command"][4:],
                      "test_exit_code": report["test_exit_code"],
                      "violations": len(report["lint_violations"]),
                      "report_file": report["report_file"],
                      "reasons": report["failure_reasons"]}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
