#!/usr/bin/env python3
"""Independent QA worker: run trusted pinned tests, then sign an exact-SHA receipt.

Run only on an ISOLATED, trusted QA host from a reviewed ProjectFlow checkout.
Never put PROJECTFLOW_QUALITY_ATTESTATION_KEY in a coding agent's environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
from agent_quality_task_gate import load_task_policy, run_task_gate  # noqa: E402
from app.services.quality_attestation_pilot import (  # noqa: E402
    PILOT_POLICY_TASK, payload_from_machine_report, sign_receipt,
)

TRUSTED_ROOT = SCRIPT_ROOT.parent


def _git(repo: Path, *argv: str) -> str:
    result = subprocess.run(["git", *argv], cwd=repo, capture_output=True,
                            text=True, check=True, timeout=10)
    return result.stdout.strip()


def independent_attest(*, worktree: Path, exact_sha: str,
                       projectflow_task_id: int, repository_id: int,
                       repository_name: str, destination: Path,
                       signing_key: str) -> dict:
    """Do not sign agent-authored reports or an unclean/moving checkout."""
    worktree = worktree.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(worktree) or destination.is_relative_to(TRUSTED_ROOT):
        raise ValueError("Receipt must be outside agent worktree and trusted source tree")
    if not worktree.is_dir() or not repository_name or projectflow_task_id < 1 or repository_id < 1:
        raise ValueError("Valid QA worktree/task/repository identity required")
    if _git(worktree, "rev-parse", "HEAD") != exact_sha:
        raise ValueError("QA worktree does not match the exact source commit")
    if _git(worktree, "status", "--porcelain"):
        raise ValueError("QA worktree is not clean")
    policy, _ = load_task_policy(PILOT_POLICY_TASK, TRUSTED_ROOT)
    target_policy, _ = load_task_policy(PILOT_POLICY_TASK, worktree)
    if target_policy != policy:
        raise ValueError("QA task policy differs from trusted verifier policy")
    # Untrusted PR code cannot weaken the required test or its setup.
    for name in ["config/agent_quality_tasks_pilot.json",
                 *policy["test_files"], "tests/conftest.py", "pyproject.toml"]:
        if (TRUSTED_ROOT / name).read_bytes() != (worktree / name).read_bytes():
            raise ValueError(f"QA infrastructure changed; reviewer must re-approve: {name}")
    with tempfile.TemporaryDirectory(prefix="projectflow-qa-pilot-") as temp:
        report = run_task_gate(PILOT_POLICY_TASK, root=worktree,
                               report_dir=Path(temp))
        if report["status"] != "PASS":
            raise ValueError(f"QA did not pass: {report['failure_reasons']}")
        if _git(worktree, "rev-parse", "HEAD") != exact_sha or _git(worktree, "status", "--porcelain"):
            raise ValueError("QA source changed while tests were running")
        payload = payload_from_machine_report(
            report, task_id=projectflow_task_id, repository_id=repository_id,
            repository_name=repository_name)
        receipt = sign_receipt(payload, signing_key)
        if destination.is_symlink():
            raise ValueError("Receipt directory must not be a symlink")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        result_path = destination / f"task-{projectflow_task_id}-repo-{repository_id}.json"
        if result_path.is_symlink():
            raise ValueError("Refusing to overwrite symlink receipt")
        # Exclusive temp file and atomic rename: merge readers never see
        # partially written JSON. Exclude key and raw test logs.
        fd, temp_path = tempfile.mkstemp(dir=destination, prefix=".quality-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(receipt, out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, result_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return {"status": "ATTESTED", "task_id": projectflow_task_id,
                "repository_id": repository_id, "git_head": exact_sha,
                "tests": policy["test_files"], "receipt": str(result_path)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worktree", type=Path, required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--task-id", type=int, required=True)
    p.add_argument("--repository-id", type=int, required=True)
    p.add_argument("--repository-name", required=True)
    p.add_argument("--receipt-dir", type=Path, required=True)
    a = p.parse_args()
    key = os.environ.get("PROJECTFLOW_QUALITY_ATTESTATION_KEY", "")
    try:
        result = independent_attest(
            worktree=a.worktree, exact_sha=a.commit,
            projectflow_task_id=a.task_id, repository_id=a.repository_id,
            repository_name=a.repository_name, destination=a.receipt_dir,
            signing_key=key)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
