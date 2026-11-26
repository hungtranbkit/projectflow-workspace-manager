"""Validation for run-scoped artifacts emitted by the pinned Visual QA CLI.

This validates consistency and freshness, not cryptographic attestation of a
repository command. The TestRunner stores the canonical manifest digest in
the existing test_runs stdout record after validation; gates recheck it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import zlib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


UIFIX_VERSION = "1.2.6"
UIFIX_COMMIT = "97c4b9d83684c7d607cb8c4b146e3f8aa5356e53"
ARTIFACT_NAMES = {"reference.png", "actual.png", "diff.png", "overlay.png", "metrics.json", "report.md"}
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
VISUAL_IMAGE_ARTIFACTS = frozenset({"reference.png", "actual.png", "diff.png", "overlay.png"})


class VisualEvidenceError(ValueError):
    """Evidence is absent, malformed, stale, tampered, or inconsistent."""


class VisualEvidenceMissing(VisualEvidenceError):
    """No evidence directory/manifest exists for the requested run."""


def canonical_route(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or not parsed.port:
        raise VisualEvidenceError("visual route is not an approved loopback HTTP URL")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return urlunsplit(("http", f"{host}:{parsed.port}", parsed.path or "/", parsed.query, ""))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_no_duplicate_keys(text: str):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise VisualEvidenceError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(VisualEvidenceError(f"non-finite JSON value: {value}")))
    except VisualEvidenceError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VisualEvidenceError(f"evidence JSON is corrupt: {exc}") from exc


def _regular_file(path: Path) -> bytes:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise VisualEvidenceMissing(f"evidence artifact missing: {path.name}") from exc
    if stat.S_ISLNK(mode):
        raise VisualEvidenceError(f"symlink artifact is forbidden: {path.name}")
    if not stat.S_ISREG(mode):
        raise VisualEvidenceError(f"artifact is not a regular file: {path.name}")
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise VisualEvidenceError(f"artifact size exceeds limit: {path.name}")
    return path.read_bytes()


def _png_dimensions(data: bytes, label: str) -> tuple[int, int]:
    if len(data) < 45 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise VisualEvidenceError(f"corrupt PNG signature: {label}")
    offset, seen_ihdr, seen_iend, idat_started, idat_ended = 8, False, False, False, False
    width = height = bit_depth = color_type = interlace = 0
    idat = bytearray()
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + size
        if end > len(data):
            raise VisualEvidenceError(f"truncated PNG chunk: {label}")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise VisualEvidenceError(f"PNG CRC mismatch: {label}")
        if kind == b"IHDR":
            if seen_ihdr or offset != 8 or size != 13:
                raise VisualEvidenceError(f"invalid PNG IHDR: {label}")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if not width or not height or compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise VisualEvidenceError(f"invalid PNG dimensions/encoding: {label}")
            seen_ihdr = True
        elif kind == b"IDAT":
            if not seen_ihdr or idat_ended:
                raise VisualEvidenceError(f"invalid PNG IDAT order: {label}")
            idat_started = True
            idat.extend(payload)
        elif idat_started and kind != b"IEND":
            idat_ended = True
        if kind == b"IEND":
            if size != 0 or not idat_started or end != len(data):
                raise VisualEvidenceError(f"invalid PNG IEND/trailing data: {label}")
            seen_iend = True
            break
        offset = end
    if not seen_ihdr or not seen_iend or not idat:
        raise VisualEvidenceError(f"incomplete PNG: {label}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or bit_depth not in (1, 2, 4, 8, 16):
        raise VisualEvidenceError(f"unsupported PNG color format: {label}")
    if color_type in (2, 4, 6) and bit_depth < 8:
        raise VisualEvidenceError(f"invalid PNG bit depth: {label}")
    bpp = max(1, (channels * bit_depth + 7) // 8)
    passes = [(0, 0, 1, 1)] if interlace == 0 else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2),
    ]
    rows = 0
    expected_size = 0
    pass_rows = []
    for x0, y0, dx, dy in passes:
        pw = max(0, (width - x0 + dx - 1) // dx)
        ph = max(0, (height - y0 + dy - 1) // dy)
        if pw and ph:
            rows += ph
            expected_size += ph * (1 + (pw * channels * bit_depth + 7) // 8)
            pass_rows.append((ph, 1 + (pw * channels * bit_depth + 7) // 8))
    if expected_size > 200 * 1024 * 1024:
        raise VisualEvidenceError(f"PNG decoded-size limit exceeded: {label}")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(bytes(idat), expected_size + 1)
        if decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise VisualEvidenceError(f"PNG image stream has trailing or oversized data: {label}")
    except zlib.error as exc:
        raise VisualEvidenceError(f"PNG image stream is corrupt: {label}") from exc
    if len(raw) != expected_size:
        raise VisualEvidenceError(f"PNG decoded-size mismatch: {label}")
    cursor = 0
    for row_count, row_size in pass_rows:
        for _ in range(row_count):
            filter_type = raw[cursor]
            if filter_type > 4:
                raise VisualEvidenceError(f"PNG row filter is invalid: {label}")
            cursor += row_size
    return width, height


def _safe_directory(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise VisualEvidenceMissing(f"evidence directory missing: {current.name}")
        if stat.S_ISLNK(mode):
            raise VisualEvidenceError("symlink in evidence directory path")
        if current == path and not stat.S_ISDIR(mode):
            raise VisualEvidenceError("evidence root is not a directory")


def ensure_private_directory(path: Path) -> None:
    """Create coordinator storage components without following symlinks."""
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            mode = current.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise VisualEvidenceError("coordinator evidence path contains a non-directory or symlink")
    path.chmod(0o700)


def validate_visual_qa_run(
    evidence_root: Path, *, run_id: int, tested_commit: str, repo_root: Path,
    contract: dict, expected_manifest_sha256: str | None = None,
) -> dict:
    """Validate one output tree against the current project contract.

    The caller chooses the coordinator-owned run path; no path from the
    command's manifest is followed. Files are rehashed on each gate read.
    """
    evidence_root, repo_root = Path(evidence_root).absolute(), Path(repo_root).resolve()
    _safe_directory(evidence_root)
    viewports = contract.get("viewports")
    if not isinstance(viewports, list) or not viewports:
        raise VisualEvidenceError("contract has no required viewports")
    expected_by_key = {}
    for viewport in viewports:
        key = f"{viewport['width']}x{viewport['height']}"
        if key in expected_by_key:
            raise VisualEvidenceError(f"duplicate contract viewport: {key}")
        reference_name = contract["reference"].get(key)
        if not reference_name:
            raise VisualEvidenceError(f"contract reference missing for viewport {key}")
        reference_path = repo_root / reference_name
        if reference_path.is_symlink() or not reference_path.is_file():
            raise VisualEvidenceError(f"configured reference is not a regular repo file: {key}")
        reference_bytes = reference_path.read_bytes()
        if _png_dimensions(reference_bytes, f"reference {key}") != (viewport["width"], viewport["height"]):
            raise VisualEvidenceError(f"configured reference dimensions mismatch: {key}")
        expected_by_key[key] = {
            "viewport": viewport,
            "reference_name": reference_name,
            "reference_sha256": _sha(reference_bytes),
        }
    try:
        children = list(evidence_root.iterdir())
    except FileNotFoundError as exc:
        raise VisualEvidenceMissing("evidence run is absent") from exc
    if any(child.is_symlink() or not child.is_dir() for child in children):
        raise VisualEvidenceError("run root contains an unexpected file or symlink")
    actual_keys = {child.name for child in children}
    if actual_keys != set(expected_by_key):
        raise VisualEvidenceError("evidence viewport set does not match contract")

    manifest_hashes = []
    metrics_by_viewport = []
    artifact_hashes_by_viewport = {}
    for key, spec in expected_by_key.items():
        directory = evidence_root / key
        _safe_directory(directory)
        manifest_path = directory / "manifest.json"
        manifest_bytes = _regular_file(manifest_path)
        manifest_hash = _sha(manifest_bytes)
        manifest = _json_no_duplicate_keys(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise VisualEvidenceError("manifest JSON must be an object")
        if {child.name for child in directory.iterdir()} != ARTIFACT_NAMES | {"manifest.json"}:
            raise VisualEvidenceError(f"artifact inventory differs from pinned CLI output: {key}")
        if manifest.get("schemaVersion") != 1 or manifest.get("runId") != str(run_id):
            raise VisualEvidenceError("manifest run identity/schema mismatch")
        if manifest.get("testedCommit") != tested_commit or manifest.get("sourceCommit") != tested_commit:
            raise VisualEvidenceError("manifest source commit mismatch")
        if manifest.get("tool") != "UIFix" or manifest.get("toolVersion") != UIFIX_VERSION or manifest.get("toolCommit") != UIFIX_COMMIT:
            raise VisualEvidenceError("manifest UIFix pin mismatch")
        if manifest.get("route") != canonical_route(contract["route"]):
            raise VisualEvidenceError("manifest route mismatch")
        if manifest.get("viewport") != spec["viewport"] or manifest.get("deviceScaleFactor") != 1:
            raise VisualEvidenceError("manifest viewport/DPR mismatch")
        if manifest.get("captureMode") != "viewport":
            raise VisualEvidenceError("manifest capture mode mismatch")
        threshold = contract["threshold"]
        if type(manifest.get("threshold")) not in (int, float) or not math.isfinite(manifest["threshold"]) or manifest["threshold"] != threshold:
            raise VisualEvidenceError("manifest threshold mismatch")
        if manifest.get("referenceSha256") != spec["reference_sha256"]:
            raise VisualEvidenceError("manifest reference hash mismatch")
        if manifest.get("passed") is not True:
            raise VisualEvidenceError("manifest does not report PASS")
        if not isinstance(manifest.get("artifacts"), dict) or set(manifest["artifacts"]) != ARTIFACT_NAMES:
            raise VisualEvidenceError("manifest artifact inventory is incomplete or unexpected")
        file_bytes = {}
        for name in ARTIFACT_NAMES:
            item = manifest["artifacts"][name]
            if not isinstance(item, dict):
                raise VisualEvidenceError("artifact entry must be an object")
            declared_name = item.get("file", name)
            if declared_name != name or Path(declared_name).name != declared_name or "/" in declared_name or "\\" in declared_name:
                raise VisualEvidenceError("unsafe artifact path")
            data = _regular_file(directory / name)
            if type(item.get("size")) is not int or item["size"] != len(data):
                raise VisualEvidenceError(f"artifact size mismatch: {name}")
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))) or _sha(data) != item["sha256"]:
                raise VisualEvidenceError(f"artifact SHA-256 mismatch: {name}")
            file_bytes[name] = data
        metrics = _json_no_duplicate_keys(file_bytes["metrics.json"].decode("utf-8"))
        if not isinstance(metrics, dict):
            raise VisualEvidenceError("metrics JSON must be an object")
        if metrics.get("schemaVersion") != 1 or metrics.get("toolCommit") != UIFIX_COMMIT or metrics.get("toolVersion") != UIFIX_VERSION:
            raise VisualEvidenceError("metrics tool/schema pin mismatch")
        if metrics.get("sourceCommit") != tested_commit or metrics.get("url") != canonical_route(contract["route"]):
            raise VisualEvidenceError("metrics source/route mismatch")
        if metrics.get("referenceSha256") != spec["reference_sha256"] or metrics.get("actualSha256") != _sha(file_bytes["actual.png"]):
            raise VisualEvidenceError("metrics image hash mismatch")
        if metrics.get("viewport") != spec["viewport"] or metrics.get("imageDimensions") != spec["viewport"] or metrics.get("deviceScaleFactor") != 1:
            raise VisualEvidenceError("metrics dimensions/viewport/DPR mismatch")
        if metrics.get("captureMode") != "viewport" or type(metrics.get("threshold")) not in (int, float) or metrics["threshold"] != threshold:
            raise VisualEvidenceError("metrics capture/threshold mismatch")
        percent = metrics.get("percentDiff")
        mismatched, total = metrics.get("mismatchedPixels"), metrics.get("totalPixels")
        ssim = metrics.get("ssim")
        if (type(percent) not in (int, float) or not math.isfinite(percent)
                or type(mismatched) is not int or type(total) is not int or total != spec["viewport"]["width"] * spec["viewport"]["height"]
                or not 0 <= mismatched <= total or abs(percent - mismatched / total) > max(1 / total, 1e-9)
                or type(ssim) not in (int, float) or not math.isfinite(ssim) or not 0 <= ssim <= 1):
            raise VisualEvidenceError("metrics values are malformed or non-finite")
        if percent > threshold:
            raise VisualEvidenceError("metrics exceed configured threshold")
        if metrics.get("passed") is not True or metrics.get("horizontalOverflow") is not False:
            raise VisualEvidenceError("metrics pass/overflow verdict is invalid")
        if metrics.get("pageErrors") != [] or metrics.get("blockedRequests") != []:
            raise VisualEvidenceError("metrics contain page errors or blocked external requests")
        if manifest.get("metrics") != {k: metrics[k] for k in ("percentDiff", "mismatchedPixels", "totalPixels", "ssim", "horizontalOverflow", "pageErrors", "blockedRequests")}:
            raise VisualEvidenceError("manifest and metrics values disagree")
        for name, image in (("reference.png", file_bytes["reference.png"]), ("actual.png", file_bytes["actual.png"]),
                            ("diff.png", file_bytes["diff.png"]), ("overlay.png", file_bytes["overlay.png"])):
            if _png_dimensions(image, name) != (spec["viewport"]["width"], spec["viewport"]["height"]):
                raise VisualEvidenceError(f"PNG dimensions mismatch: {name}")
        if _sha(file_bytes["reference.png"]) != spec["reference_sha256"]:
            raise VisualEvidenceError("stored reference does not match configured reference")
        metrics_by_viewport.append({
            "viewport": key,
            "percent_diff": percent,
            "mismatched_pixels": mismatched,
            "total_pixels": total,
            "passed": True,
        })
        artifact_hashes_by_viewport[key] = {
            name: manifest["artifacts"][name]["sha256"]
            for name in VISUAL_IMAGE_ARTIFACTS
        }
        manifest_hashes.append(f"{key}:{manifest_hash}")
    digest = _sha("\n".join(manifest_hashes).encode())
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise VisualEvidenceError("stored visual evidence manifest digest no longer matches")
    return {
        "run_id": run_id,
        "tested_commit": tested_commit,
        "viewports": sorted(expected_by_key),
        "manifest_sha256": digest,
        "metrics": metrics_by_viewport,
        "artifact_sha256": artifact_hashes_by_viewport,
    }


def read_verified_visual_png(
    evidence_root: Path, *, run_id: int, tested_commit: str, repo_root: Path,
    contract: dict, expected_manifest_sha256: str, viewport: str, artifact: str,
) -> bytes:
    """Return one bounded PNG only if its exact bytes remain manifest-valid.

    Validate before and after the no-follow descriptor read. The response is
    an in-memory copy whose digest must match both verified summaries, so a
    file replacement between validation and response cannot substitute bytes.
    """
    if artifact not in VISUAL_IMAGE_ARTIFACTS:
        raise VisualEvidenceError("visual image name is not allowed")
    if not re.fullmatch(r"[0-9]{1,4}x[0-9]{1,4}", viewport):
        raise VisualEvidenceError("visual viewport is malformed")
    expected_viewports = {f"{item['width']}x{item['height']}" for item in contract.get("viewports", [])}
    if viewport not in expected_viewports:
        raise VisualEvidenceError("visual viewport is not in the configured contract")

    before = validate_visual_qa_run(
        evidence_root, run_id=run_id, tested_commit=tested_commit,
        repo_root=repo_root, contract=contract,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    expected_hash = before["artifact_sha256"][viewport][artifact]
    artifact_path = Path(evidence_root).absolute() / viewport / artifact
    _safe_directory(artifact_path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact_path, flags)
    except OSError as exc:
        raise VisualEvidenceError("visual image is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            raise VisualEvidenceError("visual image is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_ARTIFACT_BYTES + 1)
    finally:
        os.close(descriptor)
    digest = _sha(data)
    if len(data) > MAX_ARTIFACT_BYTES or digest != expected_hash:
        raise VisualEvidenceError("visual image bytes changed after validation")

    after = validate_visual_qa_run(
        evidence_root, run_id=run_id, tested_commit=tested_commit,
        repo_root=repo_root, contract=contract,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if after["artifact_sha256"].get(viewport, {}).get(artifact) != digest:
        raise VisualEvidenceError("visual image is no longer the verified run artifact")
    spec = next(item for item in contract["viewports"] if f"{item['width']}x{item['height']}" == viewport)
    if _png_dimensions(data, artifact) != (spec["width"], spec["height"]):
        raise VisualEvidenceError("visual image dimensions do not match the contract")
    return data
