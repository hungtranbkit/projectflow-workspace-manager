from __future__ import annotations
import hashlib
import json
from pathlib import Path

import yaml

from app.services.request_memo import RequestMemo

# Track A1 (A1.4/A1.5/A1.6) perf fix, found by profiling (not guessed):
# every caller that needs the spec tree does `SpecRegistry(specs_root).
# load()` -- a BRAND NEW instance each time, so even an instance-level
# cache would do nothing. product_acceptance_service.py/
# architecture_design_service.py alone construct-and-load one 3-4 times
# per Change; at 100 Changes that is hundreds of full specs/ tree
# re-reads+re-parses for one GET /changes. `_memo` is keyed by specs_root
# path (a real cache, not per-instance) and is opt-in -- SpecRegistry's
# own documented "always re-reads from disk, so an on-disk spec edit is
# picked up without a process restart" promise stays literally true
# whenever no `with spec_registry.memoize():` scope is open (unmemoized
# by default), and even inside one, only ever collapses repeat loads of
# the SAME specs_root made within that one request/composition -- never
# held open across requests.
_memo = RequestMemo()

def memoize():
    """`with spec_registry.memoize(): ...` around one read-only HTTP
    request/composition operation. See request_memo.py."""
    return _memo.scope()

"""Spec Layer V1 (S1-S3): SpecRegistry is the ONE place that reads and
resolves the canonical specification (specs/ -- see specs/SPEC.yaml).
Deliberately independent of HTTP/DB (S3: "Keep registry logic
independent from HTTP/UI where practical") -- app/main.py wires one
instance into app.state the same way every other service here is
wired, but this module itself only ever touches the filesystem.

The file tree is the single source of truth (S1): this class never
writes to specs/, and nothing here silently becomes a second copy of
spec content -- callers that need to persist a reference (Task.spec_*,
verification_reports.spec_*) store only IDs/version numbers, resolved
back through this registry whenever they need the real text."""


class SpecError(ValueError):
    """Raised by load()/validate() -- always carries a list of concrete,
    actionable messages (S2: "Invalid specs must fail with useful,
    explicit errors"), never a bare stack trace."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


VALID_STATUSES = ("draft", "approved", "deprecated")


class SpecRegistry:
    def __init__(self, specs_root: Path | str):
        self.root = Path(specs_root)
        self.manifest: dict = {}
        self.features: dict[str, dict] = {}
        self.requirements: dict[str, dict] = {}
        self.acceptance: dict[str, dict] = {}
        self.invariants: dict[str, dict] = {}
        self._loaded = False

    # ---- loading ---------------------------------------------------
    def load(self) -> "SpecRegistry":
        """Reads SPEC.yaml + every features/*.yaml file fresh from disk,
        validates the whole tree, and populates the resolver indexes.
        Raises SpecError (never a partial/silent load, INV-003) on any
        problem. Safe to call repeatedly -- each call re-reads from
        disk, so an on-disk spec edit is picked up without a process
        restart, matching "the file tree is the canonical contract" --
        UNLESS a `with spec_registry.memoize():` scope is open (Track
        A1), in which case a repeat load() of the same specs_root within
        that one request/composition reuses the first real read."""
        cached = _memo.get(str(self.root), lambda: self._load_uncached())
        if cached is not self:
            self.manifest, self.features, self.requirements, self.acceptance, self.invariants, self._loaded = (
                cached.manifest, cached.features, cached.requirements, cached.acceptance, cached.invariants, cached._loaded)
        return self

    def _load_uncached(self) -> "SpecRegistry":
        errors: list[str] = []
        manifest_path = self.root / "SPEC.yaml"
        if not manifest_path.is_file():
            raise SpecError([f"Missing spec manifest: {manifest_path}"])
        try:
            manifest = yaml.safe_load(manifest_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise SpecError([f"{manifest_path}: invalid YAML: {exc}"])
        if not isinstance(manifest, dict):
            raise SpecError([f"{manifest_path}: must be a mapping"])

        features_dir = self.root / (manifest.get("features_dir") or "features")
        if not features_dir.is_dir():
            errors.append(f"features_dir does not exist: {features_dir}")

        features: dict[str, dict] = {}
        requirements: dict[str, dict] = {}
        acceptance: dict[str, dict] = {}
        invariants: dict[str, dict] = {}

        feature_files = sorted(features_dir.glob("*.yaml")) if features_dir.is_dir() else []
        for path in feature_files:
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError as exc:
                errors.append(f"{path}: invalid YAML: {exc}")
                continue
            if not isinstance(data, dict):
                errors.append(f"{path}: must be a mapping")
                continue
            fid = data.get("id")
            if not fid:
                errors.append(f"{path}: missing required field 'id'")
                continue
            if fid in features:
                errors.append(f"Duplicate feature id '{fid}' ({path} and {features[fid]['_path']})")
                continue
            for field in ("title", "version", "status"):
                if data.get(field) in (None, ""):
                    errors.append(f"{path} ({fid}): missing required field '{field}'")
            status = data.get("status")
            if status is not None and status not in VALID_STATUSES:
                errors.append(f"{path} ({fid}): status '{status}' must be one of {VALID_STATUSES}")
            data["_path"] = str(path)
            features[fid] = data

            for req in data.get("requirements") or []:
                rid = req.get("id") if isinstance(req, dict) else None
                if not rid:
                    errors.append(f"{path} ({fid}): a requirement is missing 'id'")
                    continue
                if rid in requirements:
                    errors.append(f"Duplicate requirement id '{rid}' ({fid} and {requirements[rid]['_feature_id']})")
                    continue
                requirements[rid] = {**req, "_feature_id": fid}
            for ac in data.get("acceptance_criteria") or []:
                aid = ac.get("id") if isinstance(ac, dict) else None
                if not aid:
                    errors.append(f"{path} ({fid}): an acceptance criterion is missing 'id'")
                    continue
                if aid in acceptance:
                    errors.append(f"Duplicate acceptance criterion id '{aid}' ({fid} and {acceptance[aid]['_feature_id']})")
                    continue
                acceptance[aid] = {**ac, "_feature_id": fid}
            for inv in data.get("invariants") or []:
                iid = inv.get("id") if isinstance(inv, dict) else None
                if not iid:
                    errors.append(f"{path} ({fid}): an invariant is missing 'id'")
                    continue
                if iid in invariants:
                    errors.append(f"Duplicate invariant id '{iid}' ({fid} and {invariants[iid]['_feature_id']})")
                    continue
                invariants[iid] = {**inv, "_feature_id": fid}

        if errors:
            raise SpecError(errors)

        self.manifest = manifest
        self.features = features
        self.requirements = requirements
        self.acceptance = acceptance
        self.invariants = invariants
        self._loaded = True
        return self

    def ensure_loaded(self) -> "SpecRegistry":
        if not self._loaded:
            self.load()
        return self

    # ---- resolution --------------------------------------------------
    def feature(self, feature_id: str) -> dict | None:
        self.ensure_loaded()
        return self.features.get(feature_id)

    def requirement(self, requirement_id: str) -> dict | None:
        self.ensure_loaded()
        return self.requirements.get(requirement_id)

    def acceptance_criterion(self, acceptance_id: str) -> dict | None:
        self.ensure_loaded()
        return self.acceptance.get(acceptance_id)

    def invariant(self, invariant_id: str) -> dict | None:
        self.ensure_loaded()
        return self.invariants.get(invariant_id)

    def feature_ids_for(self, requirement_ids=(), acceptance_ids=(), invariant_ids=()) -> set[str]:
        """Every distinct feature these IDs actually belong to -- used
        by SpecGate/SpecComplianceVerifier to catch a Task that names
        requirement/acceptance ids from a DIFFERENT feature than the one
        it declares (a real SPEC_REFERENCE_INVALID case)."""
        self.ensure_loaded()
        out: set[str] = set()
        for rid in requirement_ids:
            r = self.requirements.get(rid)
            if r:
                out.add(r["_feature_id"])
        for aid in acceptance_ids:
            a = self.acceptance.get(aid)
            if a:
                out.add(a["_feature_id"])
        for iid in invariant_ids:
            i = self.invariants.get(iid)
            if i:
                out.add(i["_feature_id"])
        return out

    # ---- baseline digest (S10) ----------------------------------------
    def baseline_digest(self) -> str:
        """Deterministic SHA256 over every spec file's PARSED, re-
        normalized content (json.dumps with sort_keys -- immune to key
        order, comments, or trailing-whitespace differences in the raw
        YAML, INV-002: "never influenced by ... wall-clock time" or
        incidental formatting), keyed by each file's path relative to
        specs/ so file identity is part of the digest too. Read-only:
        never writes anything (INV-004)."""
        self.ensure_loaded()
        paths = sorted(p for p in self.root.rglob("*.yaml") if p.is_file())
        pieces = []
        for path in paths:
            rel = path.relative_to(self.root).as_posix()
            try:
                parsed = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError as exc:
                raise SpecError([f"{path}: invalid YAML while computing baseline: {exc}"])
            canonical = json.dumps(parsed, sort_keys=True, default=str)
            pieces.append(f"{rel}\n{canonical}")
        blob = "\n---\n".join(pieces)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
