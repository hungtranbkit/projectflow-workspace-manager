from __future__ import annotations

import math
import time

import pytest


def _put_lease(db, *, owner="supervisor-a", expires_at=None):
    with db.connect() as conn:
        if expires_at is None or (isinstance(expires_at, float) and math.isnan(expires_at)):
            # The production migration declares expires_at NOT NULL. Rebuild
            # only this disposable test table so malformed legacy/corrupt
            # rows still exercise fail-closed reads without touching live DB.
            conn.execute("DROP TABLE orchestration_leases")
            conn.execute(
                "CREATE TABLE orchestration_leases(lease_key TEXT PRIMARY KEY, owner_id TEXT, expires_at REAL)"
            )
        conn.execute("DELETE FROM orchestration_leases WHERE lease_key=?", ("projectflow-supervisor",))
        conn.execute(
            "INSERT INTO orchestration_leases(lease_key,owner_id,expires_at) VALUES(?,?,?)",
            ("projectflow-supervisor", owner, expires_at),
        )


@pytest.mark.parametrize(
    "expires_at, expected",
    [
        (None, False),
        (0, False),
        (-math.inf, False),
        (math.inf, False),
        (float("nan"), False),
        ("not-a-timestamp", False),
    ],
)
def test_supervisor_lease_requires_finite_future_expiry(client, expires_at, expected):
    service = client.app.state.autonomous_execution_service
    _put_lease(service.db, expires_at=expires_at)

    assert service._supervisor_lease_is_current("supervisor-a") is expected


def test_supervisor_lease_accepts_finite_future_expiry(client):
    service = client.app.state.autonomous_execution_service
    _put_lease(service.db, expires_at=time.time() + 60)

    assert service._supervisor_lease_is_current("supervisor-a") is True


@pytest.mark.parametrize("owner", ["", 7, object()])
def test_supervisor_lease_rejects_empty_or_nonstring_request_owner(client, owner):
    service = client.app.state.autonomous_execution_service
    _put_lease(service.db, expires_at=time.time() + 60)

    assert service._supervisor_lease_is_current(owner) is False


def test_supervisor_lease_rejects_missing_row_and_successor_owner(client):
    service = client.app.state.autonomous_execution_service
    assert service._supervisor_lease_is_current("missing-owner") is False

    _put_lease(service.db, expires_at=time.time() + 60)
    _put_lease(service.db, owner="successor", expires_at=time.time() + 60)
    assert service._supervisor_lease_is_current("supervisor-a") is False


def test_supervisor_lease_preserves_explicit_manual_compatibility(client):
    service = client.app.state.autonomous_execution_service
    assert service._supervisor_lease_is_current(None) is True


def test_supervisor_lease_fails_closed_when_table_is_unavailable(client):
    service = client.app.state.autonomous_execution_service
    service.db.execute("DROP TABLE orchestration_leases")

    assert service._supervisor_lease_is_current("supervisor-a") is False
