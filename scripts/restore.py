#!/usr/bin/env python3
"""P0-4 (docs/CORE_USABILITY_QUALIFICATION.md): restore a backup made
by scripts/backup.py.

SQLite is a single-file database with no safe hot-swap while another
process holds it open -- the correct, honest pattern every local
SQLite-file tool uses is "stop the app, replace the file, start the
app again", never a live in-process restore. This script enforces
that: it refuses to run against a target the app appears to still be
serving (best-effort health check against WORKSPACE_MANAGER_HOST/PORT,
same convention scripts/status.sh already uses) unless --force is
given, matching this project's own established "REFUSED, never
guessed" precedent (app/config.py's own session_secret/auth_mode
checks).

Never destroys the previous target silently: before overwriting an
existing target file, it is itself backed up to
<target>.pre-restore-<timestamp>.db, so a mistaken restore is itself
recoverable.

Usage:
    scripts/restore.py <backup-file> [--db PATH] [--force]

Exit 0 on success; non-zero with a message on stderr on any refusal or
failure -- never a partial/silent restore."""
from __future__ import annotations
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def resolve_db_path() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    db_raw = Path(os.getenv("WORKSPACE_MANAGER_DB", "data/workspace-manager.db"))
    return (db_raw if db_raw.is_absolute() else project_root / db_raw).resolve()


def app_appears_running() -> bool:
    """Same health-check convention as scripts/status.sh -- best-effort
    only (a remote/different-host deployment won't be caught this way),
    which is why this is a --force-overridable safety net, not the sole
    guard. Never raises: any connection failure means 'cannot confirm
    it's running', not 'confirmed stopped'."""
    host = os.getenv("WORKSPACE_MANAGER_HOST", "127.0.0.1")
    port = os.getenv("WORKSPACE_MANAGER_PORT", "8765")
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            return "<title>ProjectFlow Workspace Manager</title>" in body
    except Exception:
        return False


def validate_backup_file(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"REFUSED: backup file not found: {path}")
    try:
        conn = sqlite3.connect(str(path))
        try:
            (result,) = conn.execute("PRAGMA integrity_check").fetchone()
            if result != "ok":
                raise SystemExit(f"REFUSED: backup file failed PRAGMA integrity_check: {result}")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            required = {"schema_migrations", "repositories", "tasks", "organizations"}
            missing = required - tables
            if missing:
                raise SystemExit(f"REFUSED: backup file is missing expected ProjectFlow tables: {sorted(missing)}")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        # A file that exists but isn't a SQLite database at all (e.g.
        # corrupted, truncated, or never a real backup) raises here
        # rather than from PRAGMA/SELECT above -- caught explicitly so
        # this is still a clean REFUSED, never a raw traceback.
        raise SystemExit(f"REFUSED: backup file is not a valid SQLite database: {exc}") from None


def main(argv: list[str]) -> int:
    args = list(argv)
    if not args or args[0].startswith("--"):
        print("REFUSED: usage: scripts/restore.py <backup-file> [--db PATH] [--force]", file=sys.stderr)
        return 2
    backup_path = Path(args.pop(0)).resolve()
    target = resolve_db_path()
    force = False
    while args:
        flag = args.pop(0)
        if flag == "--db" and args:
            target = Path(args.pop(0)).resolve()
        elif flag == "--force":
            force = True
        else:
            print(f"REFUSED: unknown argument {flag!r}", file=sys.stderr)
            return 2

    validate_backup_file(backup_path)

    if not force and app_appears_running():
        print(
            f"REFUSED: ProjectFlow appears to be running and serving "
            f"http://{os.getenv('WORKSPACE_MANAGER_HOST', '127.0.0.1')}:"
            f"{os.getenv('WORKSPACE_MANAGER_PORT', '8765')}/ right now. "
            "Stop it first (scripts/stop.sh), or pass --force if you have "
            "independently confirmed it is safe to proceed.",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_copy = target.with_name(f"{target.stem}.pre-restore-{stamp}{target.suffix}")
        shutil.copy2(target, safety_copy)
        print(f"Existing target preserved at {safety_copy}", file=sys.stderr)

    shutil.copy2(backup_path, target)

    # Re-verify the file now sitting at `target` opens cleanly -- never
    # declare success on the strength of "the copy call didn't raise".
    conn = sqlite3.connect(str(target))
    try:
        (result,) = conn.execute("PRAGMA integrity_check").fetchone()
        if result != "ok":
            raise SystemExit(f"RESTORE FAILED post-copy integrity_check: {result}")
    finally:
        conn.close()

    print(str(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
