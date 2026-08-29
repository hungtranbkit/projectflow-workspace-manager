from __future__ import annotations
import re

"""Conservative, explicit parser for the exact agent-completion-report
format (templates/agent-completion-report.md) as it appears in an
AgentSession's own terminal transcript. Section 4 of the spec this
implements is explicit: "Do NOT rely solely on scraping arbitrary
terminal prose... make parsing conservative and explicit... Never treat
random occurrence of the word READY as completion." This never
auto-submits anything -- it only ever offers a detected report for a
human to explicitly confirm via the real /verification-report route,
which still enforces its own rules (clean worktree, etc.)."""

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]|[\x00-\x08\x0b\x0c\x0e-\x1f]")

FIELDS = ["WORK_STATUS", "WHAT_CHANGED", "AUTOMATED_TESTS", "HOW_TO_VERIFY", "EXPECTED_RESULT", "TEST_DATA", "RUNTIME_REQUIREMENTS", "RISKS"]
REQUIRED_FIELDS = ("WORK_STATUS", "WHAT_CHANGED")


def strip_ansi(text: str) -> str:
    """Removes ANSI escape/control sequences from a transcript for
    DISPLAY purposes only (activity summaries, report detection) --
    never applied to the actual bytes sent to the browser's xterm.js
    stream (section 21)."""
    if not text:
        return text
    return ANSI_RE.sub("", text)


def _clean_field_body(text: str) -> str:
    """The LAST field's body has no following marker to bound it, so it
    can run on into whatever the agent's next terminal turn happens to
    be (e.g. its own next delivered prompt) -- cut at the first line
    that looks like the start of unrelated content (a markdown heading,
    which never appears inside a real field's own value in this
    project's report format) as a cheap, safe truncation heuristic."""
    lines = text.strip("\n").splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            lines = lines[:i]
            break
    return "\n".join(line.rstrip() for line in lines).strip()


def parse_completion_report(transcript: str) -> dict | None:
    """Finds the LAST well-formed completion-report block in a transcript
    (an agent may print an earlier draft, then a final one -- the most
    recent wins, matching "what the agent currently says now"). Requires
    every field marker to appear as its own line ("FIELD:" or "FIELD"),
    case-sensitive, exactly matching the documented format -- a stray
    mention of the word READY in unrelated prose never matches this.
    Returns None if the required fields (WORK_STATUS, WHAT_CHANGED)
    aren't both present with real content, or WORK_STATUS isn't exactly
    READY or FIX_REQUIRED."""
    if not transcript:
        return None
    # A real pty transcript uses CRLF line endings -- normalize before
    # the line-anchored marker regex below, or "FIELD:\r" never matches
    # "^FIELD:?[ \t]*$".
    clean = strip_ansi(transcript).replace("\r\n", "\n").replace("\r", "\n")
    marker_re = re.compile(r"^(" + "|".join(FIELDS) + r"):?[ \t]*$", re.MULTILINE)
    matches = list(marker_re.finditer(clean))
    if not matches:
        return None
    # Group consecutive marker+body pairs into blocks; a "block" is any
    # run of markers not interrupted by >1 blank paragraph of unrelated
    # prose containing none of the markers -- simplest robust rule: take
    # the LAST occurrence of each field, provided WORK_STATUS's last
    # occurrence and WHAT_CHANGED's last occurrence are within a few
    # thousand characters of each other (the same report, not two
    # unrelated mentions far apart in a long session).
    last_by_field: dict[str, tuple[int, int]] = {}
    for i, m in enumerate(matches):
        field = m.group(1)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(clean)
        last_by_field[field] = (body_start, body_end)
    if not all(f in last_by_field for f in REQUIRED_FIELDS):
        return None
    positions = [last_by_field[f][0] for f in REQUIRED_FIELDS]
    if max(positions) - min(positions) > 8000:
        return None  # too far apart to plausibly be the same report
    result = {}
    for field in FIELDS:
        if field in last_by_field:
            start, end = last_by_field[field]
            result[field] = _clean_field_body(clean[start:end])
    work_status = (result.get("WORK_STATUS") or "").strip().upper()
    if work_status not in ("READY", "FIX_REQUIRED"):
        return None
    if not result.get("WHAT_CHANGED"):
        return None
    result["WORK_STATUS"] = work_status
    return result
