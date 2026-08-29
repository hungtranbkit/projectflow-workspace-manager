"""Spec Layer V1 (S1-S3): SpecRegistry loads/validates specs/ -- the
file tree is the canonical specification. These tests build disposable
spec trees under tmp_path (never touching the repo's real specs/,
except the one dogfood regression test at the bottom that loads the
real tree ProjectFlow ships) so they stay fast and fully deterministic."""
from __future__ import annotations
import json

import pytest
import yaml

from app.services.spec_registry import SpecError, SpecRegistry

REAL_SPECS_ROOT = None


def _write_manifest(root, features_dir="features"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SPEC.yaml").write_text(
        f"schema_version: 1\nproject: demo\nfeatures_dir: {features_dir}\n"
    )


def _write_feature(root, path, **overrides):
    data = {
        "id": "FEAT-DEMO",
        "title": "Demo Feature",
        "version": 1,
        "status": "approved",
        "requirements": [{"id": "REQ-D1", "text": "must do the thing"}],
        "acceptance_criteria": [{"id": "AC-D1", "text": "the thing is done"}],
        "invariants": [{"id": "INV-D1", "text": "the thing stays done"}],
    }
    data.update(overrides)
    fpath = root / "features" / path
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(yaml.safe_dump(data, sort_keys=False))
    return fpath


def test_valid_spec_registry_loads(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "demo.yaml")

    registry = SpecRegistry(root).load()
    assert set(registry.features) == {"FEAT-DEMO"}
    assert set(registry.requirements) == {"REQ-D1"}
    assert set(registry.acceptance) == {"AC-D1"}
    assert set(registry.invariants) == {"INV-D1"}
    assert registry.feature("FEAT-DEMO")["title"] == "Demo Feature"
    assert registry.requirement("REQ-D1")["_feature_id"] == "FEAT-DEMO"
    assert registry.feature("NOPE") is None


def test_missing_spec_manifest_raises(tmp_path):
    root = tmp_path / "specs"
    root.mkdir()
    with pytest.raises(SpecError) as exc:
        SpecRegistry(root).load()
    assert any("SPEC.yaml" in e for e in exc.value.errors)


def test_missing_features_dir_raises(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root, features_dir="nowhere")
    with pytest.raises(SpecError) as exc:
        SpecRegistry(root).load()
    assert any("features_dir does not exist" in e for e in exc.value.errors)


def test_duplicate_feature_id_raises(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "a.yaml")
    _write_feature(root, "b.yaml")  # same FEAT-DEMO id, different file
    with pytest.raises(SpecError) as exc:
        SpecRegistry(root).load()
    assert any("Duplicate feature id" in e for e in exc.value.errors)


def test_duplicate_requirement_id_across_features_raises(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "a.yaml", id="FEAT-A", requirements=[{"id": "REQ-SHARED", "text": "x"}])
    _write_feature(root, "b.yaml", id="FEAT-B", requirements=[{"id": "REQ-SHARED", "text": "y"}])
    with pytest.raises(SpecError) as exc:
        SpecRegistry(root).load()
    assert any("Duplicate requirement id" in e for e in exc.value.errors)


def test_feature_missing_required_field_raises(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "a.yaml", status=None)
    with pytest.raises(SpecError) as exc:
        SpecRegistry(root).load()
    assert any("missing required field 'status'" in e for e in exc.value.errors)


def test_feature_invalid_status_raises(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "a.yaml", status="not-a-real-status")
    with pytest.raises(SpecError) as exc:
        SpecRegistry(root).load()
    assert any("must be one of" in e for e in exc.value.errors)


def test_feature_ids_for_resolves_across_kinds(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "a.yaml")
    registry = SpecRegistry(root).load()
    assert registry.feature_ids_for(requirement_ids=["REQ-D1"]) == {"FEAT-DEMO"}
    assert registry.feature_ids_for(acceptance_ids=["AC-D1"]) == {"FEAT-DEMO"}
    assert registry.feature_ids_for(invariant_ids=["INV-D1"]) == {"FEAT-DEMO"}
    assert registry.feature_ids_for(requirement_ids=["REQ-UNKNOWN"]) == set()


def test_baseline_digest_is_deterministic_and_content_sensitive(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "a.yaml")

    digest1 = SpecRegistry(root).load().baseline_digest()
    digest2 = SpecRegistry(root).load().baseline_digest()  # fresh instance, fresh read
    assert digest1 == digest2

    # key ordering / formatting differences must NOT change the digest
    # (S10 INV-002: a pure function of parsed content, not raw bytes).
    raw = (root / "features" / "a.yaml").read_text()
    reordered = yaml.safe_dump(yaml.safe_load(raw), sort_keys=True)
    (root / "features" / "a.yaml").write_text(reordered)
    digest3 = SpecRegistry(root).load().baseline_digest()
    assert digest3 == digest1

    # an actual content change must change the digest.
    _write_feature(root, "a.yaml", title="Renamed Demo Feature")
    digest4 = SpecRegistry(root).load().baseline_digest()
    assert digest4 != digest1


def test_baseline_digest_never_writes_to_disk(tmp_path):
    root = tmp_path / "specs"
    _write_manifest(root)
    _write_feature(root, "a.yaml")
    before = sorted(p.name for p in root.rglob("*"))
    SpecRegistry(root).load().baseline_digest()
    after = sorted(p.name for p in root.rglob("*"))
    assert before == after


def test_real_shipped_spec_layer_feature_is_valid():
    """Dogfood regression (S1): the real specs/ tree this repo ships
    must itself always load cleanly and contain the Spec Layer's own
    approved FeatureSpec with the exact shape SpecGate/SpecCompliance
    tests below rely on."""
    from pathlib import Path
    specs_root = Path(__file__).resolve().parent.parent / "specs"
    registry = SpecRegistry(specs_root).load()
    feature = registry.feature("FEAT-SPEC-LAYER")
    assert feature is not None
    assert feature["status"] == "approved"
    assert "REQ-001" in registry.requirements
    assert "AC-001" in registry.acceptance
    assert "INV-001" in registry.invariants
    # a real, non-empty digest -- proves the whole tree parses.
    assert len(registry.baseline_digest()) == 64
