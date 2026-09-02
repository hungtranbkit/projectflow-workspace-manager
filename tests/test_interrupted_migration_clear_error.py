"""P0 (docs/CORE_USABILITY_QUALIFICATION.md failure/recovery matrix:
'interrupted migration'): a real B4-phase incident, reproduced again
here directly. Database.init() applies each migration's SQL via
executescript() -- SQLite auto-commits DDL per-statement, so a script
that fails partway through leaves earlier statements' effects
committed but this version never gets marked applied. The NEXT
init() call then retries the WHOLE script from statement 1, which
fails with a raw "duplicate column"/"table already exists" error that
names neither the stuck migration version nor what actually happened.
Real sqlite3, real Database.init(), a genuinely broken 2-statement
migration injected for this test only (monkeypatched into app.db.
MIGRATIONS) -- never simulated/mocked at the exception level."""
from __future__ import annotations
import sqlite3

import pytest

import app.db as db_module
from app.db import Database


def test_interrupted_migration_gives_a_clear_actionable_error_on_retry(tmp_path, monkeypatch):
    db_path = tmp_path / "interrupted.db"
    broken_migration = (99999, "CREATE TABLE _im_probe(id INTEGER PRIMARY KEY); "
                                "ALTER TABLE _im_probe_typo ADD COLUMN x TEXT;")
    monkeypatch.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS + [broken_migration])

    db1 = Database(db_path)
    with pytest.raises(sqlite3.OperationalError) as first:
        db1.init()
    # First attempt: the real underlying SQLite error (table genuinely
    # doesn't exist) -- confirms the failure is real, not pre-empted.
    assert "no such table" in str(first.value).lower()

    # Simulate a real restart: a brand-new Database instance against
    # the SAME (now partially-migrated) file.
    db2 = Database(db_path)
    with pytest.raises(sqlite3.OperationalError) as retry:
        db2.init()
    message = str(retry.value)
    # The retry's own raw SQLite symptom would otherwise be a bare
    # "table already exists" with zero context -- the fix's whole point
    # is that this is no longer opaque: it names the exact stuck
    # migration version and explains what actually happened.
    assert "99999" in message, f"error must name the exact stuck migration version, got: {message}"
    assert "interrupted" in message.lower(), f"error must explain this is a partially-applied migration, got: {message}"

    # And the underlying real table from the migration's FIRST
    # statement really is there -- proving this is a genuine partial-
    # apply scenario, not a hypothetical.
    conn = sqlite3.connect(str(db_path))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "_im_probe" in tables
    conn.close()
