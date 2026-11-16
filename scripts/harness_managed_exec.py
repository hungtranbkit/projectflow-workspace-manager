"""Local, opt-in, fail-closed Harness child executor backed by systemd user services.

No arbitrary shell, no privileges, no ChatGPT/production monitor modifications.
systemd RuntimeMaxSec and KillMode=control-group enforce a hard process deadline
even when the Python reconciler is not running. Only owned unit names persisted
in the Harness SQLite DB may be inspected/stopped/reset by this module.
"""
from __future__ import annotations

import argparse
from contextlib import closing
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
import time
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


def archive_known_generated(repo: Path, state_root: Path, task_id: int) -> bool:
    """Clean only *new*, narrowly known agent/cache extras after preserving bytes.

    Launch requires a clean repository; never delete arbitrary untracked files.
    A post-run unknown source file or a modified tracked file blocks cleanup.
    """
    tracked = subprocess.run(["git", "-C", str(repo), "diff", "--quiet"], timeout=10)
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"], timeout=10)
    if tracked.returncode or staged.returncode:
        return False
    others = subprocess.run(["git", "-C", str(repo), "ls-files", "--others",
                             "--exclude-standard", "-z"], capture_output=True, timeout=10)
    if others.returncode:
        return False
    names = [p.decode("utf-8") for p in others.stdout.split(b"\x00") if p]
    if not names:
        return True
    files: list[tuple[Path, str]] = []
    for rel in names:
        parts = Path(rel).parts
        raw = repo / rel
        if raw.is_symlink() or any(
                (repo / Path(*parts[:i])).is_symlink()
                for i in range(1, len(parts))):
            return False
        candidate = raw.resolve()
        if repo not in candidate.parents or not candidate.is_file():
            return False
        is_cache = ("__pycache__" in parts and candidate.suffix == ".pyc")
        is_pytest = ".pytest_cache" in parts
        is_note = rel == "PROJECT_CONTEXT.md" and candidate.stat().st_size < 1000000
        if not (is_cache or is_pytest or is_note):
            return False
        files.append((candidate, rel))
    retained = state_root / "retained" / str(task_id)
    retained.mkdir(mode=0o700, parents=True, exist_ok=True)
    index = []
    for candidate, rel in files:
        saved = retained / rel
        saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(candidate, saved)
        src_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if hashlib.sha256(saved.read_bytes()).hexdigest() != src_sha:
            return False
        index.append({"path": rel, "sha256": src_sha})
    atomic_json(retained / "manifest.json", {"task_id": task_id, "archived": index})
    for candidate, rel in files:
        saved = retained / rel
        if hashlib.sha256(candidate.read_bytes()).digest() != hashlib.sha256(saved.read_bytes()).digest():
            return False
        candidate.unlink()
    # Only empty known cache directories are pruned, never user directories.
    for folder in [repo / ".pytest_cache", repo / "tests" / "__pycache__", repo / "__pycache__"]:
        if folder.is_symlink():
            return False  # Never enter an ignored symlink to another directory
        if folder.is_dir():
            for current, dirs, files_here in os.walk(folder, topdown=False, followlinks=False):
                path = Path(current)
                if (path.is_symlink() or repo not in path.resolve().parents
                        or any((path / child).is_symlink() for child in dirs)):
                    return False
                if not dirs and not files_here:
                    path.rmdir()
    after = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
    return after.returncode == 0 and not after.stdout.strip()


def preserve_run_evidence(folder: Path, task_id: int) -> bool:
    """Retain original artifacts for audit instead of deleting accepted refs.

    The owner-only state directory is persistent; deleting a temporary unit
    must not destroy the result, patch, test proof or audit logs.
    """
    names = ("job.json", "result.json", "changes.patch", "test-output.txt",
             "agent.log", "agent-output.txt", "heartbeat.json", "dispatch-receipt.json",
             "qa-source-manifest.json")
    record = {}
    for name in names:
        file = folder / name
        if file.is_symlink():
            return False
        if not file.exists():
            continue
        if not file.is_file() or file.stat().st_size > 50_000_000:
            return False
        record[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    if "job.json" not in record:
        return False
    manifest = folder / "evidence-manifest.json"
    if manifest.exists():
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            return saved == {"task_id": task_id, "files": record}
        except (OSError, ValueError):
            return False
    atomic_json(manifest, {"task_id": task_id, "files": record})
    return manifest.is_file()


def verify_coding_commit(row: sqlite3.Row, report: dict[str, Any], folder: Path) -> bool:
    """Independently recheck DB-pinned base, test and exact committed files."""
    try:
        repo = Path(row["repo"]).resolve(strict=True)
        base = row["base_sha"]
        test_sha = row["test_sha"]
        allow = json.loads(row["approved_files_json"])
        spec = json.loads((folder / "job.json").read_text(encoding="utf-8"))
        test_rel = spec["expected"]
        if (not re.fullmatch(r"[0-9a-f]{40}", base)
                or not test_sha or not isinstance(allow, list)
                or not allow or not isinstance(test_rel, str)
                or hashlib.sha256(test_rel.encode()).hexdigest() != row["expected_sha"]
                or not test_rel.startswith("tests/") or "/../" in test_rel):
            return False
        test = repo / test_rel
        if (test.is_symlink() or not test.is_file() or repo not in test.resolve().parents
                or hashlib.sha256(test.read_bytes()).hexdigest() != test_sha):
            return False
        def git_bytes(*args: str) -> bytes:
            p = subprocess.run(["git", "-C", str(repo), "-c", "core.fsmonitor=false",
                                *args], capture_output=True, timeout=10)
            if p.returncode:
                raise HarnessError("cannot verify committed Git state")
            return p.stdout.strip()
        head = git_bytes("rev-parse", "HEAD").decode()
        prev = git_bytes("rev-parse", "HEAD^").decode()
        if head != report.get("commit_sha") or prev != base:
            return False
        changed = git_bytes("diff", "--name-only", "-z", base, head)
        changed_names = {v.decode() for v in changed.split(b"\x00") if v}
        if not changed_names or not changed_names.issubset(set(allow)):
            return False
        committed_patch = git_bytes("diff", "--binary", base, head)
        patch_file = folder / "changes.patch"
        if (patch_file.is_symlink() or not patch_file.is_file()
                or committed_patch != patch_file.read_bytes().strip()):
            return False
        # Prove the committed blob is byte-identical to the immutable
        # snapshot that Docker actually tested, rather than trusting the
        # agent's patch summary or a path-only precommit recheck.
        tested = report.get("tested_sources_sha")
        snapshot = folder / "test-snapshot"
        if (not isinstance(tested, dict) or set(tested) != set(allow)
                or not snapshot.is_dir() or snapshot.is_symlink()
                or report.get("tested_test_sha") != test_sha):
            return False
        pinned_test = snapshot / test_rel
        if (pinned_test.is_symlink() or not pinned_test.is_file()
                or snapshot not in pinned_test.resolve().parents
                or hashlib.sha256(pinned_test.read_bytes()).hexdigest() != test_sha):
            return False
        for rel, expected_digest in tested.items():
            checked = snapshot / rel
            if (checked.is_symlink() or not checked.is_file()
                    or snapshot not in checked.resolve().parents
                    or hashlib.sha256(checked.read_bytes()).hexdigest() != expected_digest):
                return False
            blob = subprocess.run(
                ["git", "-C", str(repo), "-c", "core.fsmonitor=false",
                 "show", f"{head}:{rel}"], capture_output=True, timeout=10)
            if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != expected_digest:
                return False
        # No later mutation of the accepted tracked code may occur unnoticed.
        if git_bytes("diff", "--name-only", "HEAD"):
            return False
        if git_bytes("diff", "--cached", "--name-only", "HEAD"):
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError,
            json.JSONDecodeError, subprocess.TimeoutExpired, HarnessError):
        return False


def verified_log_progress(
    db_path: Path, task_id: int, log_bytes: int, observed_at: datetime,
) -> datetime:
    """Durably track actual worker output growth, not periodic heartbeat alone.

    A heartbeat confirms a PID; it does NOT certify useful agent progress.
    After ten minutes without new output, ManagedHarness marks STALLED while
    retaining the owned process and original hard deadline for safe review.
    """
    if type(log_bytes) is not int or log_bytes < 0 or log_bytes > 500_000_000:
        raise HarnessError("invalid worker log byte count")
    if observed_at.tzinfo is None:
        raise HarnessError("worker progress requires timezone")
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS harness_managed_progress (
            task_id INTEGER PRIMARY KEY,
            last_log_bytes INTEGER NOT NULL,
            last_growth_at TEXT NOT NULL
        )""")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT last_log_bytes,last_growth_at FROM harness_managed_progress WHERE task_id=?",
            (task_id,)).fetchone()
        if row is None:
            progress = observed_at
            db.execute("""INSERT INTO harness_managed_progress
                (task_id,last_log_bytes,last_growth_at) VALUES (?,?,?)""",
                (task_id, log_bytes, progress.isoformat()))
        elif log_bytes > row[0]:
            progress = observed_at
            db.execute("""UPDATE harness_managed_progress SET
                last_log_bytes=?,last_growth_at=? WHERE task_id=?""",
                (log_bytes, progress.isoformat(), task_id))
        else:
            progress = datetime.fromisoformat(row[1])
            if progress.tzinfo is None or progress > observed_at:
                raise HarnessError("invalid historical worker progress timestamp")
    return progress


class ManagedHarness:
    """Durable one-job ownership and reconciliation; no generic process kill."""

    def __init__(self, db_path: str | Path, state_root: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = HarnessStore(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS harness_managed_units (
                task_id INTEGER PRIMARY KEY,
                unit TEXT NOT NULL UNIQUE,
                run_dir TEXT NOT NULL,
                repo TEXT NOT NULL,
                executor TEXT NOT NULL,
                expected_sha TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                base_sha TEXT NOT NULL DEFAULT '',
                test_sha TEXT NOT NULL DEFAULT '',
                approved_files_json TEXT NOT NULL DEFAULT '[]'
            )""")
            existing = {r[1] for r in db.execute("PRAGMA table_info(harness_managed_units)")}
            for column in ("base_sha", "test_sha", "approved_files_json"):
                if column not in existing:
                    spec = "TEXT NOT NULL DEFAULT '[]'" if column == "approved_files_json" else "TEXT NOT NULL DEFAULT ''"
                    db.execute(f"ALTER TABLE harness_managed_units ADD COLUMN {column} {spec}")

    def _rows(self) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM harness_managed_units ORDER BY task_id").fetchall()

    def launch(self, *, main_goal: str, child_goal: str, expected: str,
               repo: str, executor: str = "codex", ttl_seconds: int = 600,
               allowed_files: list[str] | None = None) -> dict[str, Any]:
        if not 0 < ttl_seconds <= MAX_SECONDS:
            raise HarnessError("hard TTL must be between 1s and 24h")
        if executor not in {"codex", "codex-task", "zgh-node-java-qa", "echo-probe", "sleep-probe"}:
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
        base = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        if base.returncode or not re.fullmatch(r"[0-9a-f]{40}", base.stdout.strip()):
            raise HarnessError("cannot establish immutable base commit")
        base_sha = base.stdout.strip()
        test_sha = ""
        approved: list[str] = []
        if executor == "codex-task":
            if not isinstance(allowed_files, list) or not 1 <= len(allowed_files) <= 10:
                raise HarnessError("coding requires explicit 1-10 source file allowlist")
            approved = [str(p) for p in allowed_files]
            if len(set(approved)) != len(approved):
                raise HarnessError("duplicate approved source path")
            for rel in approved:
                path = project / rel
                if (not rel or rel.startswith("/") or any(x in {".", ".."} for x in rel.split("/"))
                    or not rel.endswith(".py") or not path.is_file() or path.is_symlink()
                    or path.resolve().parent != project and project not in path.resolve().parents
                    or any(x in {"tests", "test", ".github", ".venv", "migrations"} for x in Path(rel).parts)
                    or Path(rel).name.startswith("test_") or Path(rel).name.endswith("_test.py")
                    or Path(rel).name in {"conftest.py", "setup.py", "config.py", "settings.py"}):
                    raise HarnessError("unsafe or ambiguous allowlisted source path")
                tracked = subprocess.run(["git", "-C", str(project), "ls-files",
                                          "--error-unmatch", "--", rel],
                                         capture_output=True, timeout=10)
                if tracked.returncode:
                    raise HarnessError("approved path must already be tracked")
            branch = subprocess.run(["git", "-C", str(project), "branch", "--show-current"],
                                    capture_output=True, text=True, timeout=10)
            if branch.returncode or not branch.stdout.strip().startswith("harness/"):
                raise HarnessError("coding tasks require an isolated harness/* branch")
            if (expected.startswith("/") or any(x in {".", ".."} for x in expected.split("/"))
                    or not expected.startswith("tests/") or not Path(expected).name.startswith("test_")
                    or not expected.endswith(".py")):
                raise HarnessError("coding requires one canonical tests/test_*.py path")
            raw_test = project / expected
            acceptance = raw_test.resolve()
            if (raw_test.is_symlink() or project not in acceptance.parents
                    or not acceptance.is_file() or acceptance != raw_test
                    or any(x.is_symlink() for x in [project / Path(*Path(expected).parts[:i])
                                                   for i in range(1,len(Path(expected).parts))])):
                raise HarnessError("unsafe test symlink or file outside repo")
            tracked = subprocess.run(["git", "-C", str(project), "ls-files",
                                      "--error-unmatch", "--", expected],capture_output=True,timeout=10)
            if tracked.returncode:
                raise HarnessError("acceptance test must be tracked")
            test_sha = hashlib.sha256(acceptance.read_bytes()).hexdigest()
        if executor == "zgh-node-java-qa":
            from scripts.harness_zgh_node_java_qa import RUNNER
            if expected != RUNNER or allowed_files not in (None, []):
                raise HarnessError("QA accepts only the pinned ZGH Node/JDK21 test script")
            if Path(project).is_symlink() or Path("/home/dell/workspace") not in project.parents:
                raise HarnessError("ZGH QA requires an isolated Dell workspace checkout")
            origin = subprocess.run(["git", "-C", str(project), "remote", "get-url", "origin"],
                                    capture_output=True, text=True, timeout=10)
            if origin.returncode or origin.stdout.strip() not in {
                    "https://github.com/hungtranbkit/zalo-group-hub.git",
                    "https://github.com/hungtranbkit/zalo-group-hub",
                    "git@github.com:hungtranbkit/zalo-group-hub.git"}:
                raise HarnessError("QA requires exact reviewed repository origin")
            branch = subprocess.run(["git", "-C", str(project), "branch", "--show-current"],
                                    capture_output=True, text=True, timeout=10)
            if branch.returncode or not branch.stdout.strip().startswith("harness/"):
                raise HarnessError("QA requires isolated harness/* Git branch")
            source_script = project / RUNNER
            if source_script.is_symlink() or not source_script.is_file():
                raise HarnessError("pinned ZGH script missing or symlink")
            tracked = subprocess.run(["git", "-C", str(project), "ls-files",
                                      "--error-unmatch", "--", RUNNER],
                                     capture_output=True, timeout=10)
            if tracked.returncode:
                raise HarnessError("QA script must already be tracked")
            test_sha = hashlib.sha256(source_script.read_bytes()).hexdigest()
        # All generated input/output stays outside the Git worktree.
        now = utcnow()
        task = self.store.create(
            key=f"managed-{secrets.token_hex(10)}", main_goal=main_goal,
            child_goal=child_goal, dod=("tracked patch + pytest PASS + local commit" if executor == "codex-task" else "exact challenge + zero exit + independent output SHA"),
            agent=executor, node="dell-linux", created_at=now,
            ttl=timedelta(seconds=ttl_seconds))
        token = secrets.token_hex(5)
        unit = f"harness-child-{task.id}-{token}.service"
        run_dir = self.state_root / "runs" / f"{task.id}-{token}"
        run_dir.mkdir(mode=0o700, parents=True)
        spec_path = run_dir / "job.json"
        spec = {
            "task_id": task.id, "unit": unit, "executor": executor,
            "repo": str(project), "expected": expected, "prompt": child_goal, "run_dir": str(run_dir),
            "created_at": now.isoformat(), "ttl_seconds": ttl_seconds,
            "base_sha": base_sha, "test_sha": test_sha, "approved_files": approved,
        }
        atomic_json(spec_path, spec)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""INSERT INTO harness_managed_units
                (task_id,unit,run_dir,repo,executor,expected_sha,recorded_at,
                 base_sha,test_sha,approved_files_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (task.id, unit, str(run_dir), str(project), executor,
                 hashlib.sha256(expected.encode()).hexdigest(), now.isoformat(),
                 base_sha, test_sha, json.dumps(approved)))
        remaining_seconds = int((task.expires_at - utcnow()).total_seconds())
        if remaining_seconds < 1:
            self.store.expire(task.id, at=utcnow())
            return {"task_id": task.id, "state": "EXPIRED", "unit": unit,
                    "reason": "not enough time left for an authorized dispatch"}
        command = [
            "systemd-run", "--user", f"--unit={unit.removesuffix('.service')}",
            f"--property=RuntimeMaxSec={remaining_seconds}s",
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
            atomic_json(run_dir / "dispatch-receipt.json", {
                "task_id": task.id, "unit": unit, "status": "unconfirmed",
                "returncode": p.returncode, "at": utcnow().isoformat()})
            self.store.fail(task.id, at=utcnow(), reason="systemd dispatch was not confirmed")
            # A failed receipt does not prove a child was never launched.
            # Keep WIP occupied until the owned unit is independently checked.
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute("UPDATE harness_tasks SET cleanup_state='CLEANUP_PENDING' WHERE id=?",
                           (task.id,))
            return {"task_id": task.id, "state": "FAILED", "unit": unit,
                    "reason": "systemd dispatch failed or unverified; cleanup pending"}
        # Store receipt only; NEVER call this verified RUNNING.
        self.store.dispatch(task.id, external_task_id=unit, at=utcnow())
        return {"task_id": task.id, "state": "DISPATCHED", "unit": unit,
                "deadline": task.expires_at.isoformat(), "hard_ttl_seconds": remaining_seconds}

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
                if task.state not in TERMINAL and active and pid not in {"0", ""}:
                    heartbeat = folder / "heartbeat.json"
                    backend_verified = False
                    progress_time = None
                    if heartbeat.is_file():
                        try:
                            beat = json.loads(heartbeat.read_text(encoding="utf-8"))
                            progress_time = datetime.fromisoformat(beat["observed_at"])
                            child_pid = int(beat["child_pid"])
                            cgroup = Path(f"/proc/{child_pid}/cgroup").read_text(encoding="utf-8")
                            backend_verified = (
                                beat.get("task_id") == task.id
                                and beat.get("unit") == unit and unit in cgroup
                                and progress_time.tzinfo is not None
                                and timedelta(0) <= utcnow() - progress_time <= timedelta(minutes=1)
                            )
                        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                            backend_verified = False
                    useful_progress = None
                    if backend_verified:
                        size = beat.get("log_bytes")
                        growth_at = verified_log_progress(
                            self.db_path, task.id, size, utcnow())
                        if timedelta(0) <= utcnow() - growth_at <= timedelta(minutes=10):
                            useful_progress = growth_at
                    self.store.observe(task.id, backend_state="RUNNING",
                                       process_alive=True if backend_verified else None,
                                       at=utcnow(), progress_at=useful_progress,
                                       evidence_ref=f"systemd:{unit}:pid-and-log-growth")
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
                        and status.get("Result") in {"success", ""}
                    )
                    if row["executor"] == "zgh-node-java-qa":
                        from scripts.harness_zgh_node_java_qa import PROOF, independently_verify
                        verified = (
                            verified and output.strip() == PROOF
                            and independently_verify(row, report, folder)
                        )
                    elif row["executor"] == "codex-task":
                        patch = folder / "changes.patch"
                        test_output = folder / "test-output.txt"
                        commit = subprocess.run(["git", "-C", row["repo"], "rev-parse", "HEAD"],
                                                capture_output=True, text=True, timeout=10)
                        verified = (verified
                                    and report.get("test_exit") == 0
                                    and report.get("scope_verified") is True
                                    and report.get("acceptance_path") == expected_sha
                                    and patch.is_file() and not patch.is_symlink()
                                    and patch.stat().st_size > 0
                                    and report.get("patch_sha256") == hashlib.sha256(patch.read_bytes()).hexdigest()
                                    and test_output.is_file() and not test_output.is_symlink()
                                    and commit.returncode == 0
                                    and report.get("commit_sha") == commit.stdout.strip()
                                    and verify_coding_commit(row, report, folder))
                    else:
                        verified = verified and hashlib.sha256(output.strip().encode()).hexdigest() == expected_sha
                    # Reconciliation may occur after the deadline although
                    # the worker finished before it. Use the persisted finish
                    # time, not the time at which the monitor woke up.
                    try:
                        finished = datetime.fromisoformat(report["finished_at"])
                        if finished.tzinfo is None:
                            raise ValueError("timezone required")
                    except (KeyError, ValueError, TypeError):
                        finished = utcnow()
                        verified = False
                    if finished > task.expires_at:
                        self.store.expire(task.id, at=utcnow())
                    else:
                        self.store.observe(task.id, backend_state="FINISHED",
                                           process_alive=False, at=finished,
                                           evidence_ref=f"systemd:{unit}:result")
                        if verified:
                            sha = hashlib.sha256(output.encode()).hexdigest()
                            self.store.accept(
                                task.id, at=finished, artifact_ref=str(output_file),
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
                    clean = archive_known_generated(Path(row["repo"]), self.state_root, task.id)
                    dirty = not clean
                    evidence_ok = preserve_run_evidence(folder, task.id)
                    task = self.store.cleanup(
                        task.id, at=utcnow(), executor_stopped=True,
                        no_active_refs=True, dirty_worktree=dirty,
                        evidence_preserved=evidence_ok,
                        deletion_verified=True)
                    # Keep folder as a durable, mode-0700 evidence bundle.
                    # Never delete artifact_ref, original patch or test logs.
                results.append({"task_id": task.id, "state": task.state,
                                "cleanup": self.store.get(task.id).cleanup_state,
                                "unit_result": status.get("Result"),
                                "unit_active": active})
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired,
                    HarnessError) as exc:
                results.append({"task_id": task.id, "state": task.state,
                                "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
        return results


def execute_with_heartbeat(command: list[str], spec: dict[str, Any],
                           run_dir: Path) -> int:
    """Observe an actual child PID, never an idle CLI composer or submit ACK."""
    log_file = run_dir / "agent.log"
    with open(log_file, "ab") as log:
        os.fchmod(log.fileno(), 0o600)
        process = subprocess.Popen(command, cwd=spec["repo"],
                                   stdout=log, stderr=log)
        while process.poll() is None:
            atomic_json(run_dir / "heartbeat.json", {
                "task_id": spec["task_id"], "unit": spec["unit"],
                "child_pid": process.pid, "observed_at": utcnow().isoformat(),
                "log_bytes": log_file.stat().st_size,
            })
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                continue
        return int(process.returncode)


def validate_coding_delta(project: Path, test_path: str,
                          allowed_files: list[str] | None = None) -> tuple[bool, str]:
    """Require exact preapproved tracked source paths and reject all other edits.

    A filename heuristic is never an authorization policy: codex-task must
    receive its explicit reviewed source allowlist from the immutable launch.
    """
    if not allowed_files or len(set(allowed_files)) != len(allowed_files):
        return False, "missing or duplicate source allowlist"
    if not test_path.startswith("tests/") or test_path != Path(test_path).as_posix():
        return False, "ambiguous acceptance test path"
    changed = subprocess.run(
        ["git", "-C", str(project), "diff", "--name-only", "-z"],
        capture_output=True, timeout=10)
    staged = subprocess.run(
        ["git", "-C", str(project), "diff", "--cached", "--name-only", "-z"],
        capture_output=True, timeout=10)
    others = subprocess.run(
        ["git", "-C", str(project), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True, timeout=10)
    ignored = subprocess.run(
        ["git", "-C", str(project), "ls-files", "--others", "--ignored",
         "--exclude-standard", "-z"], capture_output=True, timeout=10)
    if ignored.returncode or ignored.stdout:
        return False, "ignored untracked files prevent safe acceptance"
    if any(p.returncode for p in (changed, staged, others)):
        return False, "git diff status unavailable"
    if staged.stdout:
        return False, "unexpected staged changes"
    names = [p.decode("utf-8") for p in changed.stdout.split(b"\x00") if p]
    if not names:
        return False, "no tracked implementation change"
    for rel in names:
        if rel not in allowed_files or rel == test_path:
            return False, f"refused tracked change outside implementation allowlist: {rel}"
        path = project / rel
        if path.is_symlink() or not path.is_file() or project not in path.resolve().parents:
            return False, f"unsafe source symlink/path: {rel}"
    for path_bytes in others.stdout.split(b"\x00"):
        if not path_bytes:
            continue
        rel = path_bytes.decode("utf-8")
        p = project / rel
        parts = Path(rel).parts
        if (p.is_symlink() or not p.is_file() or project not in p.resolve().parents
                or any((project / Path(*parts[:i])).is_symlink()
                       for i in range(1, len(parts)))):
            return False, "unknown symlink or foreign untracked path"
        recognized = (
            rel == "PROJECT_CONTEXT.md"
            or ("__pycache__" in parts and p.suffix == ".pyc")
            or ".pytest_cache" in parts
        )
        if not recognized:
            return False, f"unknown untracked agent output: {rel}"
    return True, "verified exact preapproved source-only change"


def snapshot_tracked_sources(repo: Path, destination: Path) -> dict[str, str]:
    """Take an immutable-test input copy of ONLY tracked, non-symlink files.

    Never copy ignored files, secrets, external symlinks, .git hooks or caches.
    The snapshot is read-only inside Docker, and retained for independent
    commit-to-tested-tree validation.
    """
    if destination.exists():
        raise RuntimeError("test snapshot already exists")
    git = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.fsmonitor=false",
         "ls-files", "-z"], capture_output=True, timeout=10)
    if git.returncode:
        raise RuntimeError("cannot enumerate trusted source files")
    files = [x.decode() for x in git.stdout.split(b"\x00") if x]
    if not files or len(files) > 4000:
        raise RuntimeError("repository snapshot limits exceeded")
    destination.mkdir(parents=True, mode=0o755)
    hashes = {}
    total_bytes = 0
    for rel in files:
        src = repo / rel
        parts = Path(rel).parts
        if (not rel or src.is_symlink() or not src.is_file()
                or repo not in src.resolve().parents
                or any((repo / Path(*parts[:i])).is_symlink()
                       for i in range(1,len(parts)))):
            raise RuntimeError("symlink/foreign tracked file in acceptance snapshot")
        size = src.stat().st_size
        total_bytes += size
        if total_bytes > 80_000_000:
            raise RuntimeError("repository snapshot exceeds 80MB")
        dst = destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(src, dst, follow_symlinks=False)
        dst.chmod(0o644)
        hashes[rel] = hashlib.sha256(dst.read_bytes()).hexdigest()
    return hashes


def sandboxed_pytest(spec: dict[str, Any], test_path: str) -> subprocess.CompletedProcess[str]:
    """Run potentially attacker-modified pytest ONLY in a no-network container.

    No Docker socket, secrets, host home, or writable repository bind mounts
    are passed in. The runner image is locally provisioned and never pulled.
    """
    repo = Path(spec["repo"]).resolve(strict=True)
    # A tracked-only snapshot fixes the race between tests and Git commit:
    # no agent can change what pytest actually imported during acceptance.
    snapshot = Path(spec.get("test_snapshot", str(repo))).resolve(strict=True)
    if not snapshot.is_dir():
        raise RuntimeError("missing read-only source snapshot")
    image = "projectflow-harness-pytest:9.0.2"
    if not shutil.which("docker"):
        raise RuntimeError("Docker test isolation unavailable")
    image_check = subprocess.run(["docker", "image", "inspect", image],
                                 capture_output=True, timeout=10)
    if image_check.returncode:
        raise RuntimeError("pinned local pytest sandbox image missing")
    expected_image_id = "sha256:ba2a442b7b20fbdc91c6b99d545a438597c7fe255beeb94eaa6ea2056ed7d105"
    try:
        installed_image_id = json.loads(image_check.stdout)[0]["Id"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("Docker image identity unavailable")
    if installed_image_id != expected_image_id:
        raise RuntimeError("pytest Docker image digest changed without review")
    start = datetime.fromisoformat(spec["created_at"])
    if start.tzinfo is None:
        raise RuntimeError("untrusted deadline timestamp")
    remaining = int((start + timedelta(seconds=int(spec["ttl_seconds"])) - utcnow()).total_seconds())
    if remaining < 2:
        raise RuntimeError("deadline expired before sandboxed acceptance")
    test_budget = max(1, min(100, remaining - 1))
    command = [
        "docker", "run", "--rm", "--pull=never", "--network=none",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=64", "--memory=512m", "--cpus=1", "--user=65534:65534",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=64m",
        "--mount", f"type=bind,src={snapshot},dst=/repo,readonly",
        "--workdir=/repo", "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=PYTHONPYCACHEPREFIX=/tmp/pyc",
        "--env=PYTHONPATH=/repo", installed_image_id,
        "timeout", "--signal=TERM", "--kill-after=2s", f"{test_budget}s",
        "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", test_path,
    ]
    return subprocess.run(command, capture_output=True, text=True,
                          timeout=min(115, test_budget + 8))


def worker(spec_file: Path) -> int:
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    run_dir = Path(spec["run_dir"]).resolve()
    output = run_dir / "agent-output.txt"
    mode = spec["executor"]
    rc = 1
    additional: dict[str, Any] = {}
    try:
        if mode == "sleep-probe":
            rc = execute_with_heartbeat(["/usr/bin/sleep", "25"], spec, run_dir)
            output.write_text("unexpected", encoding="utf-8")
        elif mode == "echo-probe":
            output.write_text(spec["expected"] + "\n", encoding="utf-8")
            rc = 0
        elif mode == "codex-task":
            cli = shutil.which("codex")
            if not cli:
                raise RuntimeError("Codex CLI unavailable")
            rc = execute_with_heartbeat([
                cli, "exec", "--sandbox", "workspace-write", "-C", spec["repo"],
                "--output-last-message", str(output),
                spec["prompt"] + " Change ONLY the existing implementation needed for "
                + spec["expected"] + ". Do not modify tests, settings or other projects.",
            ], spec, run_dir)
            test_path = spec["expected"]
            # Check change scope and unchanged test content BEFORE running any
            # potentially untrusted code; subprocess host pytest is forbidden.
            allowed, explanation = validate_coding_delta(
                Path(spec["repo"]).resolve(), test_path, spec.get("approved_files"))
            if hashlib.sha256((Path(spec["repo"]) / test_path).read_bytes()).hexdigest() != spec["test_sha"]:
                allowed, explanation = False, "acceptance test content changed"
            additional["scope_verified"] = allowed
            additional["scope_refusal"] = None if allowed else explanation
            if not allowed:
                rc = 127
            if rc == 0:
                test_snapshot = run_dir / "test-snapshot"
                snapshot_hashes = snapshot_tracked_sources(Path(spec["repo"]).resolve(),
                                                          test_snapshot)
                additional["tested_sources_sha"] = {
                    rel: snapshot_hashes[rel] for rel in spec["approved_files"]}
                additional["tested_test_sha"] = snapshot_hashes.get(test_path)
                if additional["tested_test_sha"] != spec["test_sha"]:
                    raise RuntimeError("snapshot acceptance test changed")
                acceptance = sandboxed_pytest({**spec, "test_snapshot": str(test_snapshot)},
                                             test_path)
                (run_dir / "test-output.txt").write_text(
                    (acceptance.stdout + acceptance.stderr)[-12000:], encoding="utf-8")
                additional["test_exit"] = acceptance.returncode
                additional["acceptance_path"] = hashlib.sha256(test_path.encode()).hexdigest()
                if acceptance.returncode != 0:
                    rc = 126
            if rc == 0:
                # Detect changes made by leftover asynchronous agent actions
                # while the sandbox test ran; refusal preserves work for review.
                still_allowed, reason = validate_coding_delta(
                    Path(spec["repo"]).resolve(), test_path, spec.get("approved_files"))
                tested = additional.get("tested_sources_sha", {})
                same_bytes = all(
                    hashlib.sha256((Path(spec["repo"]) / rel).read_bytes()).hexdigest() == digest
                    for rel, digest in tested.items()
                )
                if (not still_allowed or not same_bytes or hashlib.sha256(
                        (Path(spec["repo"]) / test_path).read_bytes()).hexdigest() != spec["test_sha"]):
                    additional["scope_verified"] = False
                    additional["scope_refusal"] = reason if not still_allowed else "source changed since isolated test"
                    rc = 127
            if rc == 0:
                diff = subprocess.run(["git", "-C", spec["repo"], "diff", "--binary"],
                                      capture_output=True, text=True, timeout=10)
                patch = run_dir / "changes.patch"
                patch.write_text(diff.stdout, encoding="utf-8")
                if not diff.stdout.strip():
                    rc = 126  # no verifiable code delta
                else:
                    additional["patch_sha256"] = hashlib.sha256(patch.read_bytes()).hexdigest()
                    # Stage ONLY explicitly authorized implementation paths.
                    subprocess.run(
                        ["git", "-C", spec["repo"], "-c", "core.fsmonitor=false",
                         "add", "--", *spec["approved_files"]], check=True, timeout=10)
                    staged = subprocess.run(
                        ["git", "-C", spec["repo"], "diff", "--cached",
                         "--name-only", "-z"], capture_output=True, timeout=10)
                    staged_names = {v.decode() for v in staged.stdout.split(b"\x00") if v}
                    if staged.returncode or not staged_names or not staged_names.issubset(
                            set(spec["approved_files"])):
                        raise RuntimeError("unsafe staged changes; no commit")
                    committed = subprocess.run(
                        ["git", "-C", spec["repo"], "-c", "core.hooksPath=/dev/null",
                         "-c", "core.fsmonitor=false", "commit", "-m",
                         f"harness: finish task {spec['task_id']}"],
                        capture_output=True, text=True, timeout=15)
                    rc = committed.returncode
                    if rc == 0:
                        head = subprocess.run(["git", "-C", spec["repo"], "rev-parse", "HEAD"],
                                              capture_output=True, text=True, timeout=10)
                        additional["commit_sha"] = head.stdout.strip()
        elif mode == "zgh-node-java-qa":
            from scripts.harness_zgh_node_java_qa import PROOF, run_verified_qa
            additional = run_verified_qa(spec, run_dir)
            rc = 0 if additional["qa_pass"] is True else 126
            output.write_text((PROOF if rc == 0 else "QA_FAILED") + "\n", encoding="utf-8")
        elif mode == "codex":
            cli = shutil.which("codex")
            if not cli:
                raise RuntimeError("Codex CLI unavailable")
            rc = execute_with_heartbeat([
                cli, "exec", "--sandbox", "read-only", "-C", spec["repo"],
                "--output-last-message", str(output),
                f"Reply with EXACTLY {spec['expected']} and no other text. "
                "Do not execute tools or edit files.",
            ], spec, run_dir)
    except (OSError, RuntimeError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError, ValueError, KeyError) as exc:
        rc = 125
        additional["worker_error"] = type(exc).__name__
    if output.is_file():
        out_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    else:
        out_sha = None
    atomic_json(run_dir / "result.json", {
        "task_id": spec["task_id"], "unit": spec["unit"],
        "exit_code": rc, "output_sha256": out_sha, "finished_at": utcnow().isoformat(),
        **additional,
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
    cli.add_argument("--executor", default="codex", choices=["codex", "codex-task", "zgh-node-java-qa", "echo-probe", "sleep-probe"])
    cli.add_argument("--ttl-seconds", type=int, default=600)
    cli.add_argument("--allowed-files", nargs="*", default=None,
                     help="explicit tracked Python sources permitted for codex-task")
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
                                  executor=args.executor, ttl_seconds=args.ttl_seconds,
                                  allowed_files=args.allowed_files)
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
