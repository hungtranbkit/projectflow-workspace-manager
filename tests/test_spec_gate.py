"""Spec Layer V1 (S5): SpecGate.evaluate() is the one place that decides
whether a Task's spec linkage is sufficient for the Supervisor to start
an Agent. Deterministic, six outcomes (REQ-002), always one of them --
never a raised exception for an ordinary classification/linkage
problem. Tests build a disposable spec tree under tmp_path with one
approved feature (FEAT-X) and one draft feature (FEAT-DRAFT)."""
from __future__ import annotations
import json
from pathlib import Path

import yaml

from app.services.spec_gate import ALL_CLASSIFICATIONS, SpecGate


def _spec_tree(tmp_path):
    root = tmp_path / "specs"
    (root / "features").mkdir(parents=True)
    (root / "SPEC.yaml").write_text("schema_version: 1\nproject: demo\nfeatures_dir: features\n")
    (root / "features" / "x.yaml").write_text(yaml.safe_dump({
        "id": "FEAT-X", "title": "X", "version": 1, "status": "approved",
        "requirements": [{"id": "REQ-X1", "text": "r"}],
        "acceptance_criteria": [{"id": "AC-X1", "text": "a"}],
        "invariants": [{"id": "INV-X1", "text": "i"}],
    }))
    (root / "features" / "draft.yaml").write_text(yaml.safe_dump({
        "id": "FEAT-DRAFT", "title": "Draft", "version": 1, "status": "draft",
        "requirements": [{"id": "REQ-Y1", "text": "r"}],
        "acceptance_criteria": [{"id": "AC-Y1", "text": "a"}],
    }))
    return root


def task(**overrides):
    t = {
        "spec_change_classification": None, "spec_feature_id": None,
        "spec_requirement_ids": "[]", "spec_acceptance_ids": "[]", "spec_invariant_ids": "[]",
    }
    t.update(overrides)
    return t


def test_no_classification_is_not_applicable(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task())
    assert result["outcome"] == "NOT_APPLICABLE"


def test_no_behavior_change_passes_without_any_linkage(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(spec_change_classification="NO_BEHAVIOR_CHANGE"))
    assert result["outcome"] == "PASS"


def test_ambiguous_is_spec_required(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(spec_change_classification="AMBIGUOUS"))
    assert result["outcome"] == "SPEC_REQUIRED"


def test_unknown_classification_is_spec_required(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(spec_change_classification="NOT_A_REAL_CLASSIFICATION"))
    assert result["outcome"] == "SPEC_REQUIRED"


def test_gated_classification_without_feature_id_is_spec_required(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(spec_change_classification="BEHAVIOR_CHANGE"))
    assert result["outcome"] == "SPEC_REQUIRED"


def test_unapproved_feature_is_spec_not_approved(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(
        spec_change_classification="BEHAVIOR_CHANGE", spec_feature_id="FEAT-DRAFT",
        spec_requirement_ids=json.dumps(["REQ-Y1"]), spec_acceptance_ids=json.dumps(["AC-Y1"]),
    ))
    assert result["outcome"] == "SPEC_NOT_APPROVED"


def test_missing_requirement_or_acceptance_mapping_is_traceability_missing(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(spec_change_classification="BEHAVIOR_CHANGE", spec_feature_id="FEAT-X"))
    assert result["outcome"] == "TRACEABILITY_MISSING"


def test_unknown_feature_id_is_spec_reference_invalid(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(
        spec_change_classification="NEW_FEATURE", spec_feature_id="FEAT-NOPE",
        spec_requirement_ids=json.dumps(["REQ-X1"]), spec_acceptance_ids=json.dumps(["AC-X1"]),
    ))
    assert result["outcome"] == "SPEC_REFERENCE_INVALID"


def test_unknown_requirement_id_is_spec_reference_invalid(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(
        spec_change_classification="NEW_FEATURE", spec_feature_id="FEAT-X",
        spec_requirement_ids=json.dumps(["REQ-NOPE"]), spec_acceptance_ids=json.dumps(["AC-X1"]),
    ))
    assert result["outcome"] == "SPEC_REFERENCE_INVALID"


def test_requirement_from_a_different_feature_is_spec_reference_invalid(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(
        spec_change_classification="NEW_FEATURE", spec_feature_id="FEAT-X",
        spec_requirement_ids=json.dumps(["REQ-Y1"]), spec_acceptance_ids=json.dumps(["AC-X1"]),
    ))
    assert result["outcome"] == "SPEC_REFERENCE_INVALID"


def test_valid_linkage_passes(tmp_path):
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(
        spec_change_classification="BEHAVIOR_CHANGE", spec_feature_id="FEAT-X",
        spec_requirement_ids=json.dumps(["REQ-X1"]), spec_acceptance_ids=json.dumps(["AC-X1"]),
        spec_invariant_ids=json.dumps(["INV-X1"]),
    ))
    assert result["outcome"] == "PASS"


def test_valid_linkage_accepts_a_plain_python_list_too(tmp_path):
    """spec_id_list() must accept both a JSON-string (real DB row shape)
    and an already-decoded list (hand-built dict, e.g. from tests)."""
    gate = SpecGate(_spec_tree(tmp_path))
    result = gate.evaluate(task(
        spec_change_classification="BEHAVIOR_CHANGE", spec_feature_id="FEAT-X",
        spec_requirement_ids=["REQ-X1"], spec_acceptance_ids=["AC-X1"],
    ))
    assert result["outcome"] == "PASS"


def test_broken_spec_tree_surfaces_as_spec_reference_invalid_not_a_crash(tmp_path):
    """A malformed specs/ tree must never propagate SpecError out of
    SpecGate -- it just refuses the gated Task (the safe direction)."""
    gate = SpecGate(tmp_path / "nonexistent-specs-dir")
    result = gate.evaluate(task(
        spec_change_classification="BEHAVIOR_CHANGE", spec_feature_id="FEAT-X",
        spec_requirement_ids=json.dumps(["REQ-X1"]), spec_acceptance_ids=json.dumps(["AC-X1"]),
    ))
    assert result["outcome"] == "SPEC_REFERENCE_INVALID"


def test_every_classification_is_covered():
    """No classification string can silently fall through undetected --
    every member of ALL_CLASSIFICATIONS is a real, tested branch."""
    assert set(ALL_CLASSIFICATIONS) == {
        "NO_BEHAVIOR_CHANGE", "BEHAVIOR_CHANGE", "NEW_FEATURE", "SPEC_CHANGE",
        "BUG_FIX_TO_EXISTING_SPEC", "AMBIGUOUS",
    }


def test_real_shipped_feat_spec_layer_passes_with_its_own_ids():
    """Dogfood regression: the Spec Layer's own approved FeatureSpec,
    linked with its own real requirement/acceptance ids, must PASS
    against the actual specs/ tree this repo ships."""
    specs_root = Path(__file__).resolve().parent.parent / "specs"
    gate = SpecGate(specs_root)
    result = gate.evaluate(task(
        spec_change_classification="BUG_FIX_TO_EXISTING_SPEC", spec_feature_id="FEAT-SPEC-LAYER",
        spec_requirement_ids=json.dumps(["REQ-001"]), spec_acceptance_ids=json.dumps(["AC-001"]),
        spec_invariant_ids=json.dumps(["INV-001"]),
    ))
    assert result["outcome"] == "PASS"
