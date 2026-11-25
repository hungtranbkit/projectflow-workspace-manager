from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


_TASK_MARKER = re.compile(r"^VISUAL_QA_REQUIRED:\s*true\s*$", re.IGNORECASE | re.MULTILINE)


def task_requires_visual_qa(task: dict | None) -> bool:
    """Visual QA is task opt-in; repository configuration alone never blocks other work."""
    if not task:
        return False
    text = "\n".join(
        str(task.get(field) or "")
        for field in ("implementation_prompt", "brief_requirements", "brief_test_plan")
    )
    return bool(_TASK_MARKER.search(text))


def load_visual_qa_contract(repo: Path) -> dict | None:
    """Read and validate the optional, local-only visual QA configuration."""
    project_file = repo / "PROJECT.yaml"
    if not project_file.is_file():
        return None
    data = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("PROJECT.yaml must be a mapping")
    block = data.get("visual_qa")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ValueError("visual_qa: must be a mapping")
    if block.get("enabled") is not True:
        return None

    route = block.get("route")
    if not isinstance(route, str):
        raise ValueError("visual_qa.route must be a loopback URL")
    parsed = urlsplit(route)
    try:
        is_loopback = parsed.hostname == "localhost" or bool(parsed.hostname and ip_address(parsed.hostname).is_loopback)
    except ValueError:
        is_loopback = False
    if parsed.scheme != "http" or not is_loopback or not parsed.port:
        raise ValueError("visual_qa.route must be an http loopback URL with an explicit port")

    viewports = block.get("viewports")
    if not isinstance(viewports, list) or not viewports:
        raise ValueError("visual_qa.viewports must be a non-empty list")
    normalized_viewports = []
    seen_viewports = set()
    for viewport in viewports:
        if not isinstance(viewport, dict):
            raise ValueError("each visual_qa viewport must be a mapping")
        width, height = viewport.get("width"), viewport.get("height")
        if type(width) is not int or type(height) is not int or not (320 <= width <= 3840 and 240 <= height <= 2160):
            raise ValueError("visual_qa viewport dimensions are outside the supported range")
        key = f"{width}x{height}"
        if key in seen_viewports:
            raise ValueError(f"visual_qa.viewports contains duplicate dimensions: {key}")
        seen_viewports.add(key)
        normalized_viewports.append({"width": width, "height": height})

    refs = block.get("references")
    if refs is None:
        reference = block.get("reference")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("visual_qa.reference or visual_qa.references is required")
        if len(normalized_viewports) != 1:
            raise ValueError("multiple visual_qa.viewports require a reference for each viewport in visual_qa.references")
        refs = {f"{normalized_viewports[0]['width']}x{normalized_viewports[0]['height']}": reference}
    if not isinstance(refs, dict):
        raise ValueError("visual_qa.references must map WxH viewports to repo-relative PNG files")
    repo_root = repo.resolve()
    normalized_references = {}
    for viewport in normalized_viewports:
        key = f"{viewport['width']}x{viewport['height']}"
        reference = refs.get(key)
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"visual_qa.references requires an entry for {key}")
        relative_reference = Path(reference)
        if relative_reference.is_absolute() or ".." in relative_reference.parts:
            raise ValueError("visual_qa reference paths must be repo-relative and cannot contain '..'")
        resolved_reference = (repo / relative_reference).resolve()
        if not resolved_reference.is_relative_to(repo_root):
            raise ValueError("visual_qa reference paths must resolve inside the repository")
        if not resolved_reference.is_file():
            raise ValueError(f"visual_qa reference does not exist: {reference}")
        normalized_references[key] = reference

    threshold = block.get("threshold")
    if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
        raise ValueError("visual_qa.threshold must be between 0 and 1")

    command = (data.get("commands") or {}).get("visual_qa")
    if not isinstance(command, dict) or not command.get("command"):
        raise ValueError("enabled visual_qa requires commands.visual_qa.command")

    return {
        "reference": normalized_references,
        "route": route,
        "viewports": normalized_viewports,
        "threshold": float(threshold),
        "command": str(command["command"]),
    }


def visual_qa_prompt_lines(task: dict, repo: Path) -> list[str]:
    if not task_requires_visual_qa(task):
        return []
    try:
        contract = load_visual_qa_contract(repo)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [
            "## VISUAL QA (required by this Task)",
            f"- The repo-local visual_qa contract is invalid: {exc}",
            "- Do not claim visual QA passed; report the configuration error as a blocker.",
            "",
        ]
    if contract is None:
        return [
            "## VISUAL QA (required by this Task)",
            "- VISUAL_QA_REQUIRED is set, but PROJECT.yaml has no enabled visual_qa contract.",
            "- Do not claim visual QA passed; report the missing repo-local reference/configuration as a blocker.",
            "",
        ]
    viewports = ", ".join(f"{v['width']}x{v['height']}" for v in contract["viewports"])
    references = ", ".join(f"{size}: {path}" for size, path in contract["reference"].items())
    return [
        "## VISUAL QA (required by this Task)",
        f"- Immutable references by viewport: {references}.",
        f"- Route: {contract['route']}",
        f"- Required viewports: {viewports}",
        f"- Run: {contract['command']}",
        "- Use the report to repair only the UI mismatch, then rerun the exact reference/viewport comparison.",
        "- Keep the artifacts and metrics with the tested commit SHA; do not treat a pixel score as a substitute for behavior/accessibility review.",
        "",
    ]
