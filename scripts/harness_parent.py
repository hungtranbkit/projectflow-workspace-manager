"""Minimal single-goal Harness parent coordinator for pre-approved atomic children.

A parent never executes code itself. It dispatches at most one missing child at
a time, reconciles child evidence via the managed executor, and independently
tests the main DoD only after *every* child is accepted and cleaned. No nested
parents, no extra ChatGPT Automations, no automated rewriting of a failed goal.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness_managed_exec import ManagedHarness, MAX_SECONDS, atomic_json

PLAN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{3,48}$")
STATES_OK = {"DONE_VERIFIED"}
STATES_STOP = {"FAILED", "EXPIRED"}


def validate(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict):
        raise ValueError("parent plan must be an object")
    if not PLAN_ID.fullmatch(str(plan.get("plan_id", ""))):
        raise ValueError("invalid immutable plan_id")
    if not isinstance(plan.get("main_goal"), str) or not plan["main_goal"].strip():
        raise ValueError("main goal missing")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
        raise ValueError("1-10 atomic steps required")
    seen = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("invalid step")
        if not all(isinstance(step.get(field), str) and step[field].strip()
                   for field in ("goal", "expected", "repo", "executor")):
            raise ValueError("single goal, output, repo and executor required")
        if step["goal"] in seen or step["goal"] == plan["main_goal"]:
            raise ValueError("duplicate or compound main goal")
        seen.add(step["goal"])
        if step["executor"] not in {"echo-probe", "codex", "codex-task"}:
            raise ValueError("parent executor not approved")
        ttl = step.get("ttl_seconds", 600)
        if type(ttl) is not int or not 0 < ttl <= MAX_SECONDS:
            raise ValueError("child duration must be 1s-24h")
    final = plan.get("final_acceptance")
    if not isinstance(final, dict) or not all(
        isinstance(final.get(f), str) and final[f].strip() for f in ("repo", "test")
    ):
        raise ValueError("independent main-goal pytest acceptance required")
    acceptance_repo = Path(final["repo"]).resolve()
    test_path = (acceptance_repo / final["test"]).resolve()
    if acceptance_repo not in test_path.parents or test_path.suffix != ".py":
        raise ValueError("final acceptance must be a test in the chosen repo")
    if plan.get("enabled") is not True:
        raise ValueError("parent plan requires explicit enabled=true")


def _plan_path(root: Path, plan_id: str) -> Path:
    if not PLAN_ID.fullmatch(plan_id):
        raise ValueError("invalid plan identifier")
    return root / "parents" / f"{plan_id}.json"


def install(plan: dict[str, Any], root: Path) -> Path:
    validate(plan)
    path = _plan_path(root, plan["plan_id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise ValueError("plan already registered: immutable goal cannot be overwritten")
    # Record exact plan, no secrets, no mutable timestamps or token substitution.
    atomic_json(path, plan)
    return path


def tick_one(plan: dict[str, Any], worker: ManagedHarness, root: Path,
             *, python: str = sys.executable) -> dict[str, Any]:
    validate(plan)
    status_file = root / "parents" / f"{plan['plan_id']}.status.json"
    if status_file.is_file():
        status = json.loads(status_file.read_text(encoding="utf-8"))
        if status.get("state") == "DONE_VERIFIED":
            return status
    tasks = worker.store.list_tasks(main_goal=plan["main_goal"])
    by_child = {}
    for task in tasks:
        by_child.setdefault(task.child_goal, []).append(task)
    passed = 0
    for step in plan["steps"]:
        matches = by_child.get(step["goal"], [])
        if len(matches) > 1:
            return {"main_goal": plan["main_goal"], "state": "BLOCKED",
                    "reason": "duplicate child checkpoints need review"}
        child = matches[0] if matches else None
        if child and child.state in STATES_OK and child.cleanup_state == "CLEANED_VERIFIED":
            passed += 1
            continue
        if child:
            if child.state in STATES_STOP:
                state = "BLOCKED_RESCOPE"
                reason = "child failed/expired; analyze and reduce scope, do not restart same task"
            elif child.state == "DONE_VERIFIED":
                state = "BLOCKED_CLEANUP"
                reason = "child accepted but has not completed safe cleanup"
            else:
                state = "CHILD_OUTSTANDING"
                reason = "dispatched is not running proof; check worker evidence"
            return {"main_goal": plan["main_goal"], "state": state,
                    "passed": passed, "total": len(plan["steps"]),
                    "child": child.child_goal, "child_state": child.state,
                    "cleanup": child.cleanup_state, "reason": reason}
        # Only launch the first missing child; no speculative future tasks.
        result = worker.launch(
            main_goal=plan["main_goal"], child_goal=step["goal"],
            expected=step["expected"], repo=step["repo"],
            executor=step["executor"], ttl_seconds=step.get("ttl_seconds", 600))
        return {"main_goal": plan["main_goal"], "state": "CHILD_DISPATCHED",
                "passed": passed, "total": len(plan["steps"]),
                "child": step["goal"], "dispatch": result}

    # Every child passed and cleaned; a separate independent main DoD is
    # mandatory. Never sum passed children into a fabricated parent PASS.
    final = plan["final_acceptance"]
    repo = Path(final["repo"]).resolve()
    test_path = (repo / final["test"]).resolve()
    if not test_path.is_file() or repo not in test_path.parents:
        return {"main_goal": plan["main_goal"], "state": "BLOCKED_ACCEPTANCE",
                "reason": "independent test file is missing"}
    result = subprocess.run(
        [python, "-m", "pytest", "-q", final["test"]],
        cwd=repo, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    summary = {"main_goal": plan["main_goal"], "plan_id": plan["plan_id"],
               "passed": passed, "total": len(plan["steps"]),
               "state": "DONE_VERIFIED" if result.returncode == 0 else "BLOCKED_ACCEPTANCE",
               "final_test_exit": result.returncode,
               "checked_at": datetime.now(timezone.utc).isoformat(),
               "summary": (result.stdout + result.stderr)[-1200:]}
    if result.returncode == 0:
        atomic_json(status_file, summary)
    return summary


def tick_all(worker: ManagedHarness, root: Path, *, python: str = sys.executable) -> list[dict[str, Any]]:
    folder = root / "parents"
    if not folder.exists():
        return []
    responses = []
    used_main_goals: set[str] = set()
    for path in sorted(folder.glob("*.json")):
        if path.name.endswith(".status.json") or path.is_symlink():
            continue
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
            validate(plan)
            if plan["main_goal"] in used_main_goals:
                raise ValueError("a main goal has multiple active parent plans")
            used_main_goals.add(plan["main_goal"])
            responses.append({"plan": plan["plan_id"],
                              **tick_one(plan, worker, root, python=python)})
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            responses.append({"plan": path.stem, "state": "BLOCKED",
                              "reason": f"{type(exc).__name__}: {str(exc)[:180]}"})
    return responses


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "tick", "status"])
    parser.add_argument("--plan-file")
    parser.add_argument("--db", default=str(Path.home()/".local/state/projectflow-harness/harness-mvp.db"))
    parser.add_argument("--state-dir", default=str(Path.home()/".local/state/projectflow-harness"))
    args = parser.parse_args()
    root = Path(args.state_dir).resolve()
    worker = ManagedHarness(args.db, root)
    if args.action == "install":
        if not args.plan_file:
            parser.error("--plan-file is required")
        source = Path(args.plan_file).resolve(strict=True)
        data = json.loads(source.read_text(encoding="utf-8"))
        result = {"registered": str(install(data, root))}
    elif args.action == "tick":
        result = {"parents": tick_all(worker, root)}
    else:
        result = {"plans": sorted(p.name for p in (root / "parents").glob("*.json")
                                  if not p.name.endswith(".status.json"))}
    print("HARNESS_PARENT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
