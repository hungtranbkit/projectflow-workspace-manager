from __future__ import annotations
import hashlib
import re

"""Pure, dependency-free parsing of a required-gate's raw stdout into
individual failing-test identities, plus a stable fingerprint used to
match 'the same failure' across two different test runs (a baseline
probe and an integration run). Deliberately conservative: normalizes
only volatile noise (whitespace, addresses, large numbers, decorative
dashes), never the test's own identity -- a fingerprint match is meant
to be trustworthy evidence, not a guess (section 13: 'do NOT hide
materially different failures'). No AI/inference involved anywhere
here -- this is regex parsing of real command output only."""

_SUMMARY_NUM = re.compile(r"(\d+)\s+(passed|failed|skipped)")
_PYTEST_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)(?:\s*-\s*(.+))?$", re.M)
_PW_FAILED_HEADER = re.compile(r"^\s*(\d+)\s+failed\s*$", re.M)
_PW_FAILED_ENTRY = re.compile(r"^\s+(\S+\.spec\.\w+:\d+:\d+)\s*(?:›|>)?\s*(.*)$")
_PW_SECTION_END = re.compile(r"^\s*\d+\s+(skipped|passed)\b")
_NOISE = re.compile(r"0x[0-9a-fA-F]+|\d{4,}")


def parse_summary(stdout: str) -> dict:
    """Best-effort pass/fail/skip counts from a gate's raw stdout. Takes
    the max seen for each label so a multi-stage/multi-report log (e.g.
    a pytest summary line followed by a Playwright one) doesn't silently
    keep only the first match."""
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for n, kind in _SUMMARY_NUM.findall(stdout or ""):
        counts[kind] = max(counts[kind], int(n))
    return counts


def parse_failures(stdout: str) -> list[dict]:
    """[{"test_identifier": ..., "reason": ...}] for every distinct
    failing test found in `stdout`. Understands pytest's `FAILED
    path::test - reason` lines and Playwright's `N failed` section
    listing `file.spec.js:LINE:COL › description`. Returns [] if
    nothing recognizable is found -- callers fall back to a single
    whole-gate failure entry in that case, never silently drop it."""
    text = stdout or ""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _PYTEST_FAILED_LINE.finditer(text):
        ident = m.group(1).strip()
        reason = (m.group(2) or "").strip()
        if ident and ident not in seen:
            seen.add(ident)
            out.append({"test_identifier": ident, "reason": reason})
    if out:
        return out
    header = _PW_FAILED_HEADER.search(text)
    if not header:
        return out
    in_section = False
    for line in text.splitlines():
        if _PW_FAILED_HEADER.match(line):
            in_section = True
            continue
        if not in_section:
            continue
        if _PW_SECTION_END.match(line):
            break
        m = _PW_FAILED_ENTRY.match(line)
        if not m:
            continue
        ident = m.group(1).strip()
        reason = m.group(2).strip(" ─-—")
        if ident and ident not in seen:
            seen.add(ident)
            out.append({"test_identifier": ident, "reason": reason})
    return out


def fingerprint(test_identifier: str, reason: str) -> str:
    """A stable, short identity for 'this exact failure'. Normalizes
    only obviously-volatile noise in the reason text (addresses, large
    numbers, repeated whitespace, decorative dashes); the test
    identifier itself is kept verbatim -- if a test's own path/line
    moves, that is treated as a materially different failure on
    purpose, matching the 'never hide a materially different failure'
    rule rather than trying to be clever about renames."""
    norm_reason = re.sub(r"\s+", " ", (reason or "").strip().lower())
    norm_reason = _NOISE.sub("#", norm_reason)
    basis = f"{test_identifier.strip()}|{norm_reason}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
