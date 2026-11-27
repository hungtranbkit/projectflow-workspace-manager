from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
import re
import hashlib
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


def load_visual_qa_contract(repo: Path, *, config_bytes: bytes | None = None) -> dict | None:
    """Read and validate the optional, local-only visual QA configuration."""
    project_file = repo / "PROJECT.yaml"
    if config_bytes is None:
        if not project_file.is_file():
            return None
        data = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
    else:
        data = yaml.safe_load(config_bytes.decode("utf-8")) or {}
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


def load_committed_visual_qa_contract(repo: Path, git, tested_commit: str,
                                      *, project_bytes: bytes | None = None) -> dict | None:
    """Validate an enabled Visual QA contract from exact tracked HEAD blobs.

    Only PROJECT.yaml and the reference PNGs selected by that committed
    contract are required to match HEAD; ignored build/cache outputs remain
    allowed. The caller still checks its broader source-state policy.
    """
    from app.services.visual_qa_evidence import MAX_ARTIFACT_BYTES, _png_dimensions

    if project_bytes is None:
        try:
            project_bytes = git.read_committed_file(
                repo, tested_commit, "PROJECT.yaml", max_bytes=1024 * 1024,
            )
        except FileNotFoundError:
            # An ignored/untracked PROJECT.yaml cannot define a repository contract.
            return None
    else:
        tracked_bytes = git.read_committed_file(
            repo, tested_commit, "PROJECT.yaml", max_bytes=1024 * 1024,
        )
        if tracked_bytes != project_bytes:
            raise ValueError("PROJECT.yaml bytes differ from the tested commit")
    contract = load_visual_qa_contract(repo, config_bytes=project_bytes)
    if contract is None:
        return None
    reference_hashes = {}
    for viewport in contract["viewports"]:
        key = f"{viewport['width']}x{viewport['height']}"
        relative_path = contract["reference"][key]
        image = git.read_committed_file(
            repo, tested_commit, relative_path, max_bytes=MAX_ARTIFACT_BYTES,
        )
        if _png_dimensions(image, "committed reference") != (viewport["width"], viewport["height"]):
            raise ValueError("Committed reference PNG dimensions differ from its configured viewport")
        reference_hashes[key] = hashlib.sha256(image).hexdigest()
    source = git.source_state(repo)
    if not source["clean"] or source["head"] != tested_commit:
        raise ValueError("Visual QA contract source is no longer clean at the tested commit")
    contract["_reference_sha256"] = reference_hashes
    return contract


def read_committed_visual_reference_png(repo: Path, git, tested_commit: str,
                                        contract: dict, viewport_key: str) -> bytes:
    """Return the exact tracked reference bytes selected by a validated contract."""
    from app.services.visual_qa_evidence import MAX_ARTIFACT_BYTES, _png_dimensions

    viewport = next((item for item in contract.get("viewports", [])
                     if f"{item['width']}x{item['height']}" == viewport_key), None)
    relative_path = contract.get("reference", {}).get(viewport_key)
    if viewport is None or not isinstance(relative_path, str):
        raise ValueError("Visual QA viewport is not present in the committed contract")
    image = git.read_committed_file(
        repo, tested_commit, relative_path, max_bytes=MAX_ARTIFACT_BYTES,
    )
    if _png_dimensions(image, "committed reference") != (viewport["width"], viewport["height"]):
        raise ValueError("Committed reference PNG dimensions differ from its configured viewport")
    return image


def visual_qa_prompt_lines(task: dict, repo: Path, *, git=None, tested_commit: str | None = None) -> list[str]:
    if not task_requires_visual_qa(task):
        return []
    try:
        if git is None:
            contract = load_visual_qa_contract(repo)
        else:
            source = git.source_state(repo)
            tested_commit = tested_commit or source["head"]
            if not source["clean"] or source["head"] != tested_commit:
                raise ValueError("Visual QA prompt requires a clean current commit")
            contract = load_committed_visual_qa_contract(repo, git, tested_commit)
    except Exception:
        return [
            "## VISUAL QA (required by this Task)",
            "- The repo-local visual_qa contract is invalid or not bound to the tested commit.",
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
