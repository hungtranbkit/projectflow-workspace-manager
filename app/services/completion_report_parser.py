from __future__ import annotations
import re

"""Conservative parsers for human-submitted and executor-session reports.

The legacy parser supports the manual report form. The strict parser
recognizes only a complete delimited executor contract; its caller must
still validate Git and ProjectFlow test evidence before persisting it.
Neither parser establishes independent Review or acceptance."""

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[=>]|[\x00-\x08\x0b\x0c\x0e-\x1f]")

FIELDS = ["WORK_STATUS", "WHAT_CHANGED", "AUTOMATED_TESTS", "HOW_TO_VERIFY", "EXPECTED_RESULT", "TEST_DATA", "RUNTIME_REQUIREMENTS", "RISKS"]
REQUIRED_FIELDS = ("WORK_STATUS", "WHAT_CHANGED")
EXECUTOR_FIELDS = (*FIELDS, "FILES_CHANGED", "COMMIT_SHA")
EXECUTOR_REPORT_START = "----- PROJECTFLOW EXECUTOR REPORT START -----"
EXECUTOR_REPORT_END = "----- PROJECTFLOW EXECUTOR REPORT END -----"


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
    # A transcript may end with the exact report template ProjectFlow
    # delivered to the agent. Treat each WORK_STATUS through the next
    # WORK_STATUS as one candidate; a placeholder/empty echoed template
    # must not shadow the actual report printed earlier in the PTY.
    starts = [i for i, match in enumerate(matches) if match.group(1) == "WORK_STATUS"]
    for start_index in reversed(starts):
        block_end = next((i for i in range(start_index + 1, len(matches))
                          if matches[i].group(1) == "WORK_STATUS"), len(matches))
        block = matches[start_index:block_end]
        by_field = {}
        for i, match in enumerate(block):
            if match.group(1) not in by_field:
                end = block[i + 1].start() if i + 1 < len(block) else len(clean)
                by_field[match.group(1)] = (match.end(), end)
        if not all(field in by_field for field in REQUIRED_FIELDS):
            continue
        if max(by_field[field][0] for field in REQUIRED_FIELDS) - min(by_field[field][0] for field in REQUIRED_FIELDS) > 8000:
            continue
        result = {field: _clean_field_body(clean[start:end])
                  for field, (start, end) in by_field.items()}
        work_status = (result.get("WORK_STATUS") or "").strip().upper()
        if work_status not in ("READY", "FIX_REQUIRED") or not result.get("WHAT_CHANGED"):
            continue
        if re.search(r"(?i)\b(?:todo|tbd|placeholder|replace this|fill this)\b", result["WHAT_CHANGED"]):
            continue
        result["WORK_STATUS"] = work_status
        return result
    return None


def parse_executor_completion_report(transcript: str) -> dict | None:
    """Parse the complete machine-ingestible executor contract.

    Unlike the legacy human-confirmation parser above, this rejects missing
    or duplicate fields and cannot turn partial PTY output into evidence.
    It still does not establish that the claimed commit/tests are true; the
    caller must verify them against Git and ProjectFlow's evidence tables.
    """
    if not transcript:
        return None
    clean = strip_ansi(transcript).replace("\r\n", "\n").replace("\r", "\n")
    # Parse the last explicitly delimited final report. Prompt text and
    # earlier drafts may mention headings; only content inside the final
    # complete block can become evidence.
    start_at = clean.rfind(EXECUTOR_REPORT_START)
    if start_at < 0:
        return None
    body_start = start_at + len(EXECUTOR_REPORT_START)
    end_at = clean.find(EXECUTOR_REPORT_END, body_start)
    if end_at < 0 or EXECUTOR_REPORT_START in clean[body_start:end_at]:
        return None
    clean = clean[body_start:end_at]
    marker_re = re.compile(r"^([A-Z_]+):?[ \t]*$", re.MULTILINE)
    matches = [m for m in marker_re.finditer(clean) if m.group(1) in EXECUTOR_FIELDS]
    if not matches:
        return None
    # A single exact report block is required. Duplicate fields commonly
    # indicate a redraw/partial tail or multiple drafts and are ambiguous.
    names = [m.group(1) for m in matches]
    if names != list(EXECUTOR_FIELDS):
        return None
    result = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(clean)
        result[match.group(1)] = _clean_field_body(clean[match.end():end])
    if result["WORK_STATUS"].upper() != "READY" or any(not result[k] for k in EXECUTOR_FIELDS):
        return None
    result["WORK_STATUS"] = "READY"
    if not re.fullmatch(r"[0-9a-f]{40}", result["COMMIT_SHA"].strip(), re.I):
        return None
    if re.search(r"(?i)\b(?:todo|tbd|placeholder|fill this|n/?a)\b", "\n".join(result.values())):
        return None
    result["COMMIT_SHA"] = result["COMMIT_SHA"].strip().lower()
    return result
