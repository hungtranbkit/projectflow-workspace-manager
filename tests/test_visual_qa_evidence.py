"""Strict validation of run-scoped UIFix artifacts for opted-in tasks."""
from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest

from app.services.visual_qa_evidence import VisualEvidenceError, validate_visual_qa_run


UIFIX_SHA = "97c4b9d83684c7d607cb8c4b146e3f8aa5356e53"


def png(width=320, height=240, color=(24, 57, 90)):
    def chunk(name, payload):
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload) & 0xffffffff)

    rows = b"".join(b"\0" + bytes(color) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def expected(root, *, viewports=None):
    viewports = viewports or [{"width": 320, "height": 240}]
    refs = {}
    for viewport in viewports:
        key = f"{viewport['width']}x{viewport['height']}"
        reference = root / "docs" / f"reference-{key}.png"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(png(viewport["width"], viewport["height"]))
        refs[key] = str(reference.relative_to(root))
    return {
        "run_id": 42,
        "tested_commit": "a" * 40,
        "repo_root": root,
        "contract": {
            "reference": refs,
            "route": "http://127.0.0.1:4173/orders",
            "viewports": viewports,
            "threshold": 0.08,
        },
    }


def create_run(root: Path, exp: dict, *, manifest_overrides=None, metrics_overrides=None, artifact_overrides=None):
    viewport = exp["contract"]["viewports"][0]
    key = f"{viewport['width']}x{viewport['height']}"
    out = root / key
    out.mkdir(parents=True)
    reference = (exp["repo_root"] / exp["contract"]["reference"][key]).read_bytes()
    image = png(viewport["width"], viewport["height"])
    files = {
        "reference.png": reference,
        "actual.png": image,
        "diff.png": image,
        "overlay.png": image,
        "report.md": b"UIFix report\n",
    }
    files.update(artifact_overrides or {})
    metrics = {
        "schemaVersion": 1, "tool": "UIFix", "toolVersion": "1.2.6", "toolCommit": UIFIX_SHA,
        "sourceCommit": exp["tested_commit"], "referenceSha256": hashlib.sha256(reference).hexdigest(),
        "actualSha256": hashlib.sha256(files["actual.png"]).hexdigest(),
        "url": exp["contract"]["route"], "viewport": viewport,
        "captureMode": "viewport", "imageDimensions": viewport,
        "deviceScaleFactor": 1, "threshold": 0.08, "percentDiff": 0.0,
        "mismatchedPixels": 0, "totalPixels": viewport["width"] * viewport["height"], "ssim": 1.0,
        "horizontalOverflow": False, "pageErrors": [], "blockedRequests": [], "passed": True,
    }
    metrics.update(metrics_overrides or {})
    files["metrics.json"] = (json.dumps(metrics, allow_nan=True) + "\n").encode()
    (out / "metrics.json").write_bytes(files["metrics.json"])
    artifacts = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in files.items()}
    artifacts.update({name: {"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name, data in (artifact_overrides or {}).items()})
    manifest = {
        "schemaVersion": 1, "runId": "42", "testedCommit": exp["tested_commit"], "sourceCommit": exp["tested_commit"],
        "tool": "UIFix", "toolVersion": "1.2.6", "toolCommit": UIFIX_SHA,
        "route": exp["contract"]["route"], "referenceSha256": hashlib.sha256(reference).hexdigest(),
        "viewport": viewport, "deviceScaleFactor": 1,
        "captureMode": "viewport", "threshold": 0.08, "passed": True,
        "metrics": {k: metrics[k] for k in ("percentDiff", "mismatchedPixels", "totalPixels", "ssim", "horizontalOverflow", "pageErrors", "blockedRequests")},
        "artifacts": artifacts,
    }
    overrides = dict(manifest_overrides or {})
    artifact_metadata_overrides = overrides.pop("artifacts", {})
    manifest.update(overrides)
    manifest["artifacts"].update(artifact_metadata_overrides)
    for name, data in files.items():
        (out / name).write_bytes(data)
    (out / "manifest.json").write_text(json.dumps(manifest, allow_nan=True) + "\n", encoding="utf-8")
    return out


def test_valid_fixture_artifact_set_is_accepted_and_digest_is_returned(tmp_path):
    exp = expected(tmp_path)
    run = create_run(tmp_path / "run", exp)

    result = validate_visual_qa_run(run.parent, **exp)

    assert result["run_id"] == 42
    assert result["tested_commit"] == "a" * 40
    assert len(result["manifest_sha256"]) == 64
    assert result["viewports"] == ["320x240"]
    assert result["metrics"] == [{
        "viewport": "320x240", "percent_diff": 0.0,
        "mismatched_pixels": 0, "total_pixels": 320 * 240, "passed": True,
    }]


@pytest.mark.parametrize(
    "manifest_overrides,metrics_overrides,reason",
    [
        ({"runId": "41"}, {}, "run"),
        ({"testedCommit": "b" * 40}, {}, "commit"),
        ({"route": "http://127.0.0.1:4173/other"}, {}, "route"),
        ({"threshold": 0.01}, {}, "threshold"),
        ({"viewport": {"width": 321, "height": 240}}, {}, "viewport"),
        ({}, {"percentDiff": float("nan")}, "finite"),
        ({}, {"threshold": 0.01}, "threshold"),
        ({}, {"percentDiff": 0.2, "mismatchedPixels": 15360}, "threshold"),
        ({}, {"passed": False}, "pass"),
        ({}, {"pageErrors": ["uncaught"]}, "page"),
        ({}, {"blockedRequests": ["https://outside.invalid"]}, "request"),
    ],
)
def test_metadata_mismatch_and_falsified_or_nonfinite_metrics_are_rejected(tmp_path, manifest_overrides, metrics_overrides, reason):
    exp = expected(tmp_path)
    run = create_run(tmp_path / "run", exp, manifest_overrides=manifest_overrides, metrics_overrides=metrics_overrides)

    with pytest.raises(VisualEvidenceError, match=reason):
        validate_visual_qa_run(run.parent, **exp)


def test_manifest_digest_pin_detects_evidence_reuse_or_post_run_tampering(tmp_path):
    exp = expected(tmp_path)
    run = create_run(tmp_path / "run", exp)
    first = validate_visual_qa_run(run.parent, **exp)
    (run / "manifest.json").write_text((run / "manifest.json").read_text().replace('"runId": "42"', '"runId": "42", "tampered": true'))

    with pytest.raises(VisualEvidenceError, match="manifest digest"):
        validate_visual_qa_run(run.parent, **exp, expected_manifest_sha256=first["manifest_sha256"])


def test_missing_or_corrupt_manifest_fails_closed(tmp_path):
    exp = expected(tmp_path)
    run = tmp_path / "run" / "320x240"
    run.mkdir(parents=True)
    with pytest.raises(VisualEvidenceError, match="manifest"):
        validate_visual_qa_run(run.parent, **exp)
    (run / "manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(VisualEvidenceError, match="JSON"):
        validate_visual_qa_run(run.parent, **exp)


def test_corrupt_png_size_hash_and_path_traversal_fail(tmp_path):
    exp = expected(tmp_path)
    run = create_run(tmp_path / "run", exp, artifact_overrides={"actual.png": b"not a png"})
    with pytest.raises(VisualEvidenceError, match="size|SHA|PNG"):
        validate_visual_qa_run(run.parent, **exp)

    run = create_run(tmp_path / "run2", exp, manifest_overrides={"artifacts": {"actual.png": {"file": "../outside.png", "sha256": "0" * 64, "size": 5}}})
    with pytest.raises(VisualEvidenceError, match="inventory|path"):
        validate_visual_qa_run(run.parent, **exp)


def test_symlink_artifact_outside_run_root_is_rejected(tmp_path):
    exp = expected(tmp_path)
    run = create_run(tmp_path / "run", exp)
    (run / "actual.png").unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    (run / "actual.png").symlink_to(outside)

    with pytest.raises(VisualEvidenceError, match="symlink"):
        validate_visual_qa_run(run.parent, **exp)


def test_viewport_set_must_be_exact_and_unique(tmp_path):
    sizes = [{"width": 320, "height": 240}, {"width": 640, "height": 480}]
    exp = expected(tmp_path, viewports=sizes)
    run = create_run(tmp_path / "run", exp)
    with pytest.raises(VisualEvidenceError, match="reference|viewport"):
        validate_visual_qa_run(run.parent, **exp)
