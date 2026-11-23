#!/usr/bin/env python3
"""Strict region-level screenshot and immutable-source-asset acceptance gates.

Requires approved visual references and actual browser captures. This is not a
font recognizer, image generator or automatic visual-fidelity score.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from PIL import Image

CATEGORIES = {'icon', 'typography', 'button', 'artwork', 'background', 'layout'}
SHA256 = re.compile(r'^[0-9a-f]{64}$')


def require_number(value, name, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value or (high is not None and value > high):
        raise ValueError(f'{name} must be a number between {low} and {high if high is not None else "infinity"}')
    return value


def bounds(value, name, image_size):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f'{name} must be [x,y,width,height]')
    if any(isinstance(n, bool) or not isinstance(n, int) for n in value):
        raise ValueError(f'{name} requires integer screenshot pixels')
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > image_size[0] or y + h > image_size[1]:
        raise ValueError(f'{name} is outside the reference viewport')
    return value


def inside_project(project, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or relative.startswith('/'):
        raise ValueError('protected asset path must be a relative POSIX path')
    base = project.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or relative.startswith('../'):
        raise ValueError('protected asset path escapes project')
    return path


def audit(contract_path: Path, project: Path, out: Path):
    contract = json.loads(contract_path.read_text(encoding='utf-8'))
    if contract.get('version') != 1:
        raise ValueError('contract version must be 1')
    ref_path = inside_project(project, contract['reference'])
    render_path = inside_project(project, contract['render'])
    with Image.open(ref_path) as ref_open, Image.open(render_path) as render_open:
        ref = ref_open.convert('RGB')
        render = render_open.convert('RGB')
    if ref.size != render.size:
        raise ValueError(f'viewport mismatch: reference={ref.size}, render={render.size}; recapture at matching viewport/DPR')
    regions = contract.get('regions')
    if not isinstance(regions, list) or not regions:
        raise ValueError('regions must be a nonempty array of independent checks')
    results, failures, used = [], [], set()
    for region in regions:
        ident = region['id']
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', ident) or ident in used:
            raise ValueError(f'invalid or duplicate region id: {ident}')
        used.add(ident)
        category = region['category']
        if category not in CATEGORIES:
            raise ValueError(f'{ident}: unsupported category {category}')
        rb = bounds(region['reference_bounds'], f'{ident}.reference_bounds', ref.size)
        ab = bounds(region['render_bounds'], f'{ident}.render_bounds', render.size)
        pos_tolerance = require_number(region.get('max_position_delta_px', 0), f'{ident}.max_position_delta_px')
        size_tolerance = require_number(region.get('max_size_delta_px', 0), f'{ident}.max_size_delta_px')
        channel_tolerance = require_number(region.get('pixel_tolerance', 24), f'{ident}.pixel_tolerance', 0, 255)
        max_fraction = require_number(region['max_mismatch_fraction'], f'{ident}.max_mismatch_fraction', 0, 1)
        dx, dy = abs(rb[0]-ab[0]), abs(rb[1]-ab[1])
        dw, dh = abs(rb[2]-ab[2]), abs(rb[3]-ab[3])
        geometry_ok = max(dx, dy) <= pos_tolerance and max(dw, dh) <= size_tolerance
        # Rescaling a source crop would conceal incorrect CTA and artwork dimensions.
        comparable = rb[2:] == ab[2:]
        mismatch_fraction = None
        if comparable:
            a = ref.crop((rb[0], rb[1], rb[0]+rb[2], rb[1]+rb[3]))
            b = render.crop((ab[0], ab[1], ab[0]+ab[2], ab[1]+ab[3]))
            diff = sum(max(abs(x-y) for x, y in zip(p, q)) > channel_tolerance for p, q in zip(a.getdata(), b.getdata()))
            mismatch_fraction = diff / (rb[2]*rb[3])
        passed = geometry_ok and comparable and mismatch_fraction <= max_fraction
        item = {
            'id': ident, 'category': category, 'passed': passed,
            'reference_bounds': rb, 'render_bounds': ab,
            'position_delta_px': [dx,dy], 'size_delta_px': [dw,dh],
            'geometry_ok': geometry_ok, 'pixel_comparable': comparable,
            'mismatch_fraction': mismatch_fraction, 'max_mismatch_fraction': max_fraction
        }
        results.append(item)
        if not passed:
            failures.append(f'{ident} ({category}): geometry={geometry_ok}; pixel mismatch={mismatch_fraction} (limit {max_fraction})')
    protected = contract.get('protected_assets', [])
    if not isinstance(protected, list):
        raise ValueError('protected_assets must be an array')
    if contract.get('require_protected_assets') and not protected:
        failures.append('protected source assets required but none declared')
    asset_results = []
    for asset in protected:
        ident, relative, expected = asset['id'], asset['path'], asset['sha256']
        if not isinstance(expected, str) or not SHA256.fullmatch(expected):
            raise ValueError(f'{ident}: sha256 must be lowercase 64-digit hex')
        path = inside_project(project, relative)
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        passed = actual == expected
        asset_results.append({'id': ident, 'path': relative, 'passed': passed, 'expected_sha256': expected, 'actual_sha256': actual})
        if not passed:
            failures.append(f'{ident}: protected source asset missing or changed ({relative})')
    if contract.get('required_categories'):
        for category in contract['required_categories']:
            if category not in CATEGORIES:
                raise ValueError(f'invalid required category {category}')
            if not any(r['category'] == category for r in results):
                failures.append(f'no independently audited region for required category {category}')
    report = {'passed': not failures, 'viewport_px': list(ref.size), 'regions': results,
              'protected_assets': asset_results, 'failures': failures,
              'note': 'Requires human-reviewed bounds and real browser screenshots. Pixel differences are diagnostic, not a visual quality score.'}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', required=True, type=Path)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = audit(args.contract, args.project, args.out)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f'Fidelity contract invalid: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'passed': report['passed'], 'failures': report['failures'], 'report': str(args.out)}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
