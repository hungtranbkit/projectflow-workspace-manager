"""Pure parsing/fingerprint tests for app.services.failure_classifier --
no DB, no subprocess, no fixtures. Real sample output shapes captured
from the actual Task #5 demo (pytest's `FAILED path - reason` lines and
Playwright's `N failed` section)."""
from __future__ import annotations

from app.services.failure_classifier import fingerprint, parse_failures, parse_summary

PYTEST_SAMPLE = """
tests/test_default_admin_password.py::test_non_production_compose_profiles_use_default_admin_password PASSED
tests/test_v6584432_rbac.py::test_release_sync FAILED

=================================== FAILURES ===================================
FAILED tests/test_v6584432_rbac.py::test_release_sync - AssertionError: assert '71.0.0.65' == '71.0.0.79'
1 failed, 7 passed in 0.06s
"""

PLAYWRIGHT_SAMPLE = """
[88/88] tests/e2e/tutorial-detailed.spec.js:486:1 › MESFlow tutorial chi tiết
  1 failed
    tests/e2e/mesflow.spec.js:60:1 › ESP Kiosk tutorial loads seven runtime videos and plays ───
  4 skipped
  83 passed (1.6m)
"""


def test_parse_summary_pytest_style():
    assert parse_summary(PYTEST_SAMPLE) == {"passed": 7, "failed": 1, "skipped": 0}


def test_parse_summary_playwright_style():
    assert parse_summary(PLAYWRIGHT_SAMPLE) == {"passed": 83, "failed": 1, "skipped": 4}


def test_parse_failures_pytest_style():
    failures = parse_failures(PYTEST_SAMPLE)
    assert len(failures) == 1
    assert failures[0]["test_identifier"] == "tests/test_v6584432_rbac.py::test_release_sync"
    assert "AssertionError" in failures[0]["reason"]


def test_parse_failures_playwright_style():
    failures = parse_failures(PLAYWRIGHT_SAMPLE)
    assert len(failures) == 1
    assert failures[0]["test_identifier"] == "tests/e2e/mesflow.spec.js:60:1"
    assert "ESP Kiosk tutorial" in failures[0]["reason"]


def test_parse_failures_no_failures_returns_empty():
    assert parse_failures("83 passed, 4 skipped in 90s") == []
    assert parse_failures("") == []


def test_fingerprint_stable_for_identical_failure():
    a = fingerprint("tests/e2e/mesflow.spec.js:60:1", "ESP Kiosk tutorial loads seven runtime videos and plays")
    b = fingerprint("tests/e2e/mesflow.spec.js:60:1", "ESP Kiosk tutorial loads seven runtime videos and plays")
    assert a == b


def test_fingerprint_differs_for_a_materially_different_failure():
    a = fingerprint("tests/e2e/mesflow.spec.js:60:1", "ESP Kiosk tutorial loads seven runtime videos and plays")
    b = fingerprint("tests/e2e/mesflow.spec.js:60:1", "expected 7 videos, got 0")
    assert a != b


def test_fingerprint_differs_when_test_identifier_moves():
    """Section 13: a moved test identity is treated as a materially
    different failure on purpose, never silently reused."""
    a = fingerprint("tests/e2e/mesflow.spec.js:60:1", "same reason")
    b = fingerprint("tests/e2e/mesflow.spec.js:75:1", "same reason")
    assert a != b


def test_fingerprint_normalizes_volatile_numbers_in_reason():
    a = fingerprint("t", "took 1234567 ms at 0xdeadbeef")
    b = fingerprint("t", "took 7654321 ms at 0xfeedface")
    assert a == b
