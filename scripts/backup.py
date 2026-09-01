#!/usr/bin/env python3
"""P0-4 (docs/CORE_USABILITY_QUALIFICATION.md): the minimum production-
grade backup for ProjectFlow's own durable state.

ProjectFlow keeps exactly one source of durable truth for its own
metadata: the single SQLite file at WORKSPACE_MANAGER_DB (default
data/workspace-manager.db, same resolution rule as app/config.py's own
load_settings()). Everything else is either regenerable (git worktrees
-- recreated from the branch inside the real repo's own .git, as long
as the branch survives; settings.state_dir's sandbox environment files
-- ephemeral by design, SandboxManager/CleanupWorker already reclaim
them) or the operator's own responsibility outside ProjectFlow's scope
(the tenant git repositories themselves -- their own remote is their
durable source of truth, ProjectFlow only ever reads/writes branches
inside them).

Uses sqlite3's own online Connection.backup() API, never a raw file
copy -- this is safe to run against a DB a live ProjectFlow process
still has open (every Database.connect() call in app/db.py is a fresh,
short-lived connection, never one long-held lock), because the backup
API takes SQLite's own read lock correctly for the duration of the
copy, unlike `cp` or shutil.copy, which can copy a file mid-write and
produce a torn, unusable snapshot.

Usage:
    scripts/backup.py [--db PATH] [--out DIR]

Exit 0 on success (prints the backup path + sha256 to stdout, one line
each, for a caller/script to capture); non-zero with a message on
stderr on any failure -- never a partial/silent backup."""
from __future__ import annotations
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def resolve_db_path() -> Path:
    """Same resolution rule as app/config.py's load_settings() --
    WORKSPACE_MANAGER_DB, relative to the project root if not absolute,
    default data/workspace-manager.db -- so this script finds the exact
    same file a real running instance uses, never a guess."""
    project_root = Path(__file__).resolve().parents[1]
    db_raw = Path(os.getenv("WORKSPACE_MANAGER_DB", "data/workspace-manager.db"))
    return (db_raw if db_raw.is_absolute() else project_root / db_raw).resolve()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(src: Path, out_dir: Path) -> Path:
    if not src.is_file():
        raise SystemExit(f"REFUSED: source database not found at {src} -- nothing to back up")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"workspace-manager-{stamp}.db"
    src_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(dest))
    try:
        # A real online, consistent snapshot -- SQLite's own backup API,
        # correct even against a source another process currently has
        # open (unlike a raw file copy, see module docstring).
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    # Verify the snapshot is a real, openable, structurally sane
    # database before ever calling this a successful backup -- never
    # hand an operator a file that merely exists.
    check = sqlite3.connect(str(dest))
    try:
        (result,) = check.execute("PRAGMA integrity_check").fetchone()
        if result != "ok":
            dest.unlink(missing_ok=True)
            raise SystemExit(f"REFUSED: backup snapshot failed PRAGMA integrity_check: {result}")
        table_count = check.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if table_count == 0:
            dest.unlink(missing_ok=True)
            raise SystemExit("REFUSED: backup snapshot has zero tables -- refusing to call this a backup")
    finally:
        check.close()
    return dest


def main(argv: list[str]) -> int:
    db_path = resolve_db_path()
    out_dir = Path(__file__).resolve().parents[1] / "data" / "backups"
    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag == "--db" and args:
            db_path = Path(args.pop(0)).resolve()
        elif flag == "--out" and args:
            out_dir = Path(args.pop(0)).resolve()
        else:
            print(f"REFUSED: unknown argument {flag!r}", file=sys.stderr)
            return 2
    dest = backup(db_path, out_dir)
    digest = sha256_of(dest)
    print(str(dest))
    print(digest)
    if os.getenv("WORKSPACE_MANAGER_SECRET_ENCRYPTION_KEYS"):
        print(
            "NOTE: WORKSPACE_MANAGER_SECRET_ENCRYPTION_KEYS is set in this "
            "environment. Org secrets in this backup are encrypted with it "
            "(B0.7 design: the key itself is NEVER stored in the database). "
            "This backup alone cannot recover usable secrets without that "
            "exact key preserved separately by the operator.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
