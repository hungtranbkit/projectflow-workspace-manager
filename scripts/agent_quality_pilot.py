#!/usr/bin/env python3
"""Opt-in, local-only proof gate for agent-authored Python changes.

Pilot only: does not modify the app's integration/merge/deploy pipeline.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_COMMAND = [sys.executable, "-m", "pytest", "-q", "tests/test_agent_quality_pilot.py"]
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
UI_TYPES = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8", ".webp": b"RIFF", ".mp4": None, ".webm": b"\x1a\x45\xdf\xa3"}


def lint_python(source: str, filename: str = "<input>") -> list[dict]:
    """Return targeted AST violations, not broad stylistic judgments."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [{"file": filename, "line": exc.lineno or 1, "rule": "PY_SYNTAX", "message": exc.msg}]
    violations: list[dict] = []
    for node in ast.walk(tree):
        rule = message = None
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in {"eval", "exec"}:
                rule, message = "NO_DYNAMIC_EXEC", "Direct eval()/exec() requires an independently reviewed exception"
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                if fn.value.id == "os" and fn.attr == "system":
                    rule, message = "NO_OS_SYSTEM", "Use explicit subprocess argv instead of os.system()"
                elif fn.value.id == "subprocess" and fn.attr in {"run", "call", "Popen", "check_call", "check_output"}:
                    if any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
                        rule, message = "NO_SHELL_TRUE", "shell=True is disallowed in this pilot"
        elif isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            rule, message = "NO_SILENT_EXCEPT", "Do not swallow exceptions with a bare pass"
        if rule:
            violations.append({"file": filename, "line": node.lineno, "rule": rule, "message": message})
    return sorted(violations, key=lambda item: (item["line"], item["rule"]))


def safe_repo_file(value: str, root: Path) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.suffix != ".py":
        raise ValueError(f"Expected a Python file inside repository: {value}")
    return path


def validate_ui_proof(value: str | None, required: bool, root: Path) -> str | None:
    if not value:
        if required:
            raise ValueError("UI change needs a real screenshot/video (--ui-evidence)")
        return None
    proof = (root / value).resolve()
    if not proof.is_relative_to(root.resolve()) or not proof.is_file():
        raise ValueError("UI proof must be an existing file inside the repository")
    expected = UI_TYPES.get(proof.suffix.lower())
    if proof.suffix.lower() not in UI_TYPES or proof.stat().st_size < 64:
        raise ValueError("UI proof needs a nonempty PNG/JPG/WEBP/MP4/WEBM file")
    with proof.open("rb") as fh:
        header = fh.read(16)
    if expected is not None and not header.startswith(expected):
        raise ValueError("UI proof has an invalid file header")
    if proof.suffix.lower() == ".webp" and header[8:12] != b"WEBP":
        raise ValueError("Invalid WEBP file header")
    if proof.suffix.lower() == ".mp4" and header[4:8] != b"ftyp":
        raise ValueError("Invalid MP4 file header")
    return proof.relative_to(root.resolve()).as_posix()


def git_sha(root: Path) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False, timeout=5)
    return proc.stdout.strip() if proc.returncode == 0 else None


def quality_gate(task_id: str, file_names: list[str], *, root: Path = REPO_ROOT,
                 report_dir: Path | None = None, require_ui: bool = False,
                 ui_evidence: str | None = None, test_command: list[str] | None = None,
                 test_env: dict[str, str] | None = None) -> dict:
    """Run machine-owned checks; output files are never an agent's self-report."""
    if not TASK_RE.fullmatch(task_id):
        raise ValueError("task-id must be 1-80 safe letters/digits/dot/dash/underscore")
    if not file_names:
        raise ValueError("Provide at least one --file for the scoped pilot")
    root = root.resolve()
    report_dir = report_dir or root / "artifacts" / "agent-quality-pilot"
    report_dir = report_dir.resolve()
    if not report_dir.is_relative_to(root) and not report_dir.is_relative_to(Path('/tmp')):
        raise ValueError("Reports must be under the repository or /tmp")
    report_dir.mkdir(parents=True, exist_ok=True)
    violations = []
    for name in file_names:
        path = safe_repo_file(name, root)
        violations.extend(lint_python(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix()))
    reasons = []
    if violations:
        reasons.append("Targeted Python lint failed")
    try:
        ui_path = validate_ui_proof(ui_evidence, require_ui, root)
    except ValueError as exc:
        ui_path = None
        reasons.append(str(exc))
    command = test_command if test_command is not None else TEST_COMMAND
    # Do not interpolate a shell command or let the agent supply one via CLI.
    result = None
    if not reasons:
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=180,
                                    check=False, env=test_env)
        except subprocess.TimeoutExpired:
            reasons.append("Test command exceeded 180 seconds")
    output = (result.stdout + "\n" + result.stderr) if result is not None else "Tests not run: earlier gate failed\n"
    if result is not None and result.returncode:
        reasons.append(f"Tests exited with code {result.returncode}")
    log_file = report_dir / f"{task_id}.log"
    report_file = report_dir / f"{task_id}.json"
    log_file.write_text(output, encoding="utf-8")
    report = {
        "schema_version": 1,
        "task_id": task_id,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "PASS" if not reasons else "FAIL",
        "scope_files": file_names,
        "lint_violations": violations,
        "test_command": command,
        "test_exit_code": result.returncode if result is not None else None,
        "test_log": str(log_file),
        "ui_required": require_ui,
        "ui_evidence": ui_path,
        "git_head": git_sha(root),
        "failure_reasons": reasons,
        "limitations": "Only targeted changed Python files and the fixed pilot test suite are checked; UI evidence existence is not independent visual acceptance.",
    }
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_file"] = str(report_file)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--file", action="append", required=True, dest="files", help="Python file changed by the agent (repeatable)")
    parser.add_argument("--require-ui", action="store_true", help="Require actual UI evidence file for UI changes")
    parser.add_argument("--ui-evidence", help="PNG/JPG/WEBP/MP4/WEBM file within repo")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    try:
        report = quality_gate(args.task_id, args.files, report_dir=args.report_dir,
                              require_ui=args.require_ui, ui_evidence=args.ui_evidence)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": report["status"], "report": report["report_file"],
                      "reasons": report["failure_reasons"]}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
