"""Local, opt-in, fail-closed Harness child executor backed by systemd user services.

No arbitrary shell, no privileges, no ChatGPT/production monitor modifications.
systemd RuntimeMaxSec and KillMode=control-group enforce a hard process deadline
even when the Python reconciler is not running. Only owned unit names persisted
in the Harness SQLite DB may be inspected/stopped/reset by this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.agent_harness_mvp import HarnessStore, HarnessError

MAX_SECONDS = 24 * 3600
UNIT_RE = re.compile(r"^harness-child-[0-9]+-[a-f0-9]{10}\.service$")
TERMINAL = {"DONE_VERIFIED", "FAILED", "EXPIRED"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "x", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(obj, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def service(*arguments: str, timeout: int = 15, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *arguments], text=True,
                          capture_output=True, timeout=timeout, check=check)


def unit_status(unit: str) -> dict[str, str]:
    if not UNIT_RE.fullmatch(unit):
        raise HarnessError("refused: unit name outside Harness namespace")
    p = service("show", unit, "-p", "LoadState", "-p", "ActiveState",
                "-p", "SubState", "-p", "Result", "-p", "MainPID", "-p",
                "ExecMainStatus", "-p", "RuntimeMaxUSec", "-p", "KillMode")
    if p.returncode:
        raise HarnessError("systemctl show unavailable")
    pairs = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    return pairs


class ManagedHarness:
    """Durable one-job ownership and reconciliation; no generic process kill."""

    def __init__(self, db_path: str | Path, state_root: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = HarnessStore(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS harness_managed_units (
                task_id INTEGER PRIMARY KEY,
                unit TEXT NOT NULL UNIQUE,
                run_dir TEXT NOT NULL,
                repo TEXT NOT NULL,
                executor TEXT NOT NULL,
                expected_sha TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )""")

    def _rows(self) -> list[sqlite3.Row]:
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM harness_managed_units ORDER BY task_id").fetchall()

    def launch(self, *, main_goal: str, child_goal: str, expected: str,
               repo: str, executor: str = "codex", ttl_seconds: int = 600) -> dict[str, Any]:
        if not 0 < ttl_seconds <= MAX_SECONDS:
            raise HarnessError("hard TTL must be between 1s and 24h")
        if executor not in {"codex", "echo-probe", "sleep-probe"}:
            raise HarnessError("unsupported executor")
        if not expected or len(expected) > 2000 or not main_goal.strip() or not child_goal.strip():
            raise HarnessError("missing atomic goal / expected evidence")
        project = Path(repo).resolve(strict=True)
        if not project.is_dir() or not (project / ".git").exists():
            raise HarnessError("isolated git repository required")
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
        if status.returncode or status.stdout.strip():
            raise HarnessError("refused: dirty or unverified worktree")
        # All generated input/output stays outside the Git worktree.
        now = utcnow()
        task = self.store.create(
            key=f"managed-{secrets.token_hex(10)}", main_goal=main_goal,
            child_goal=child_goal, dod="exact challenge + zero exit + independent output SHA",
            agent=executor, node="dell-linux", created_at=now,
            ttl=timedelta(seconds=ttl_seconds))
        token = secrets.token_hex(5)
        unit = f"harness-child-{task.id}-{token}.service"
        run_dir = self.state_root / "runs" / f"{task.id}-{token}"
        run_dir.mkdir(mode=0o700, parents=True)
        spec_path = run_dir / "job.json"
        spec = {
            "task_id": task.id, "unit": unit, "executor": executor,
            "repo": str(project), "expected": expected, "run_dir": str(run_dir),
            "created_at": now.isoformat(), "ttl_seconds": ttl_seconds,
        }
        atomic_json(spec_path, spec)
        with sqlite3.connect(self.db_path) as db:
            db.execute("""INSERT INTO harness_managed_units
                (task_id,unit,run_dir,repo,executor,expected_sha,recorded_at)
                VALUES (?,?,?,?,?,?,?)""",
                (task.id, unit, str(run_dir), str(project), executor,
                 hashlib.sha256(expected.encode()).hexdigest(), now.isoformat()))
        command = [
            "systemd-run", "--user", f"--unit={unit.removesuffix('.service')}",
            f"--property=RuntimeMaxSec={ttl_seconds}s",
            "--property=KillMode=control-group",
            "--property=NoNewPrivileges=yes", "--property=Restart=no",
            f"--working-directory={project}",
            sys.executable, str(Path(__file__).resolve()), "worker", "--spec", str(spec_path),
        ]
        p = subprocess.run(command, capture_output=True, text=True, timeout=15)
        # systemd-run may write its successful unit receipt to stderr rather
        # than stdout, depending on the installed systemd version.
        receipt = (p.stdout or "") + (p.stderr or "")
        if p.returncode or f"Running as unit: {unit}" not in receipt:
            self.store.fail(task.id, at=utcnow(), reason="systemd dispatch was not confirmed")
            return {"task_id": task.id, "state": "FAILED", "unit": unit,
                    "reason": "systemd dispatch failed or unverified; cleanup pending"}
        # Store receipt only; NEVER call this verified RUNNING.
        self.store.dispatch(task.id, external_task_id=unit, at=utcnow())
        return {"task_id": task.id, "state": "DISPATCHED", "unit": unit,
                "deadline": task.expires_at.isoformat(), "hard_ttl_seconds": ttl_seconds}

    def reconcile(self) -> list[dict[str, Any]]:
        results = []
        for row in self._rows():
            task = self.store.get(row["task_id"])
            if task.cleanup_state == "CLEANED_VERIFIED":
                continue
            unit = row["unit"]
            folder = Path(row["run_dir"]).resolve()
            if not UNIT_RE.fullmatch(unit) or self.state_root not in folder.parents:
                results.append({"task_id": task.id, "error": "owned unit/path mismatch"})
                continue
            result_file = folder / "result.json"
            try:
                status = unit_status(unit)
                active = status.get("ActiveState") in {"active", "activating", "reloading"}
                pid = status.get("MainPID")
                # Never stop a service without a record of ownership.
                if utcnow() >= task.expires_at and active:
                    service("stop", unit, timeout=15)
                    status = unit_status(unit)
                    active = status.get("ActiveState") in {"active", "activating", "reloading"}
                    pid = status.get("MainPID")
                stopped = not active and pid in {"0", ""}
                if task.state not in TERMINAL and result_file.is_file() and stopped:
                    report = json.loads(result_file.read_text(encoding="utf-8"))
                    output_file = folder / "agent-output.txt"
                    output = output_file.read_text(encoding="utf-8") if output_file.is_file() else ""
                    expected_sha = row["expected_sha"]
                    verified = (
                        report.get("task_id") == task.id
                        and report.get("unit") == unit
                        and report.get("exit_code") == 0
                        and report.get("output_sha256") == hashlib.sha256(output.encode()).hexdigest()
                        and hashlib.sha256(output.strip().encode()).hexdigest() == expected_sha
                        and status.get("Result") in {"success", ""}
                    )
                    self.store.observe(task.id, backend_state="FINISHED",
                                       process_alive=False, at=utcnow(),
                                       evidence_ref=f"systemd:{unit}:result")
                    if verified:
                        sha = hashlib.sha256(output.encode()).hexdigest()
                        self.store.accept(
                            task.id, at=utcnow(), artifact_ref=str(output_file),
                            test_evidence=f"worker-exit=0:{unit}",
                            runtime_readback=f"sha256:{sha}",
                            verifier=lambda a, t, r: (
                                Path(a).is_file() and "worker-exit=0" in t
                                and r == f"sha256:{sha}"
                                and Path(a).read_text(encoding="utf-8").strip() == output.strip()
                            ))
                    else:
                        self.store.fail(task.id, at=utcnow(), reason="output/exit/systemd proof failed")
                elif task.state not in TERMINAL and utcnow() >= task.expires_at:
                    self.store.expire(task.id, at=utcnow())
                task = self.store.get(task.id)
                if task.state in TERMINAL and stopped:
                    # Discard only the transient service owned by this task.
                    if status.get("LoadState") == "loaded":
                        service("reset-failed", unit, timeout=15)
                    after = unit_status(unit)
                    if after.get("MainPID") not in {"0", ""} or after.get("ActiveState") in {"active", "activating"}:
                        results.append({"task_id": task.id, "state": task.state,
                                        "cleanup": "PENDING", "reason": "unit not stopped"})
                        continue
                    git = subprocess.run(["git", "-C", row["repo"], "status", "--porcelain"],
                                         capture_output=True, text=True, timeout=10)
                    dirty = git.returncode != 0 or bool(git.stdout.strip())
                    task = self.store.cleanup(
                        task.id, at=utcnow(), executor_stopped=True,
                        no_active_refs=True, dirty_worktree=dirty,
                        evidence_preserved=result_file.is_file() or task.state == "EXPIRED",
                        deletion_verified=True)
                    if task.cleanup_state == "CLEANED_VERIFIED":
                        # Preserve the durable database checkpoint and hashes.
                        shutil.rmtree(folder)
                results.append({"task_id": task.id, "state": task.state,
                                "cleanup": self.store.get(task.id).cleanup_state,
                                "unit_result": status.get("Result"),
                                "unit_active": active})
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired,
                    HarnessError) as exc:
                results.append({"task_id": task.id, "state": task.state,
                                "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
        return results


def worker(spec_file: Path) -> int:
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    run_dir = Path(spec["run_dir"]).resolve()
    output = run_dir / "agent-output.txt"
    mode = spec["executor"]
    rc = 1
    try:
        if mode == "sleep-probe":
            subprocess.run(["/usr/bin/sleep", "25"], check=False)
            output.write_text("unexpected", encoding="utf-8")
            rc = 0
        elif mode == "echo-probe":
            output.write_text(spec["expected"] + "\n", encoding="utf-8")
            rc = 0
        elif mode == "codex":
            cli = shutil.which("codex")
            if not cli:
                raise RuntimeError("Codex CLI unavailable")
            proc = subprocess.run([
                cli, "exec", "--sandbox", "read-only", "-C", spec["repo"],
                "--output-last-message", str(output),
                f"Reply with EXACTLY {spec['expected']} and no other text. "
                "Do not execute tools or edit files.",
            ], capture_output=True, text=True)
            rc = proc.returncode
    except (OSError, RuntimeError) as exc:
        rc = 125
    if output.is_file():
        out_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    else:
        out_sha = None
    atomic_json(run_dir / "result.json", {
        "task_id": spec["task_id"], "unit": spec["unit"],
        "exit_code": rc, "output_sha256": out_sha, "finished_at": utcnow().isoformat(),
    })
    return rc


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("action", choices=["start", "reconcile", "status", "worker"])
    cli.add_argument("--db", default=str(Path.home() / ".local/state/projectflow-harness/harness-mvp.db"))
    cli.add_argument("--state-dir", default=str(Path.home() / ".local/state/projectflow-harness"))
    cli.add_argument("--main-goal")
    cli.add_argument("--child-goal")
    cli.add_argument("--expected")
    cli.add_argument("--repo")
    cli.add_argument("--executor", default="codex", choices=["codex", "echo-probe", "sleep-probe"])
    cli.add_argument("--ttl-seconds", type=int, default=600)
    cli.add_argument("--spec")
    args = cli.parse_args()
    if args.action == "worker":
        if not args.spec:
            cli.error("--spec required")
        raise SystemExit(worker(Path(args.spec)))
    manager = ManagedHarness(args.db, args.state_dir)
    if args.action == "start":
        if not all((args.main_goal, args.child_goal, args.expected, args.repo)):
            cli.error("start requires --main-goal, --child-goal, --expected, --repo")
        response = manager.launch(main_goal=args.main_goal, child_goal=args.child_goal,
                                  expected=args.expected, repo=args.repo,
                                  executor=args.executor, ttl_seconds=args.ttl_seconds)
    elif args.action == "reconcile":
        response = {"managed": manager.reconcile()}
    else:
        response = {"tasks": [
            {"task_id": t.id, "main_goal": t.main_goal, "child_goal": t.child_goal,
             "state": t.state, "deadline": t.expires_at.isoformat(),
             "cleanup": t.cleanup_state}
            for t in manager.store.list_tasks()
        ]}
    print("HARNESS_MANAGED=" + json.dumps(response, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
