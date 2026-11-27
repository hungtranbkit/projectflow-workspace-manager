#!/usr/bin/env python3
"""Validate structural consistency and evidence provenance of a design-spec.json."""
import argparse
import json
from pathlib import PurePosixPath
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def validate(doc, schema):
    errors = [f'{"/".join(map(str, err.absolute_path)) or "$"}: {err.message}'
              for err in Draft202012Validator(schema).iter_errors(doc)]
    if errors:
        return errors
    layer_ids = [layer['id'] for layer in doc['layers']]
    asset_ids = [asset['id'] for asset in doc['assets']]
    component_ids = [comp['id'] for comp in doc['components']]
    for kind, values in [('layer', layer_ids), ('asset', asset_ids), ('component', component_ids)]:
        if len(values) != len(set(values)):
            errors.append(f'duplicate {kind} IDs')
    for layer in doc['layers']:
        if layer.get('asset_id') and layer['asset_id'] not in asset_ids:
            errors.append(f'layer {layer["id"]} uses nonexistent asset_id {layer["asset_id"]}')
        if layer['bounds'] is not None:
            x, y, w, h = layer['bounds']
            if w <= 0 or h <= 0 or x+w > doc['source']['width_px'] or y+h > doc['source']['height_px']:
                errors.append(f'layer {layer["id"]} bounds outside reference image')
    for component in doc['components']:
        if component['bounds'] is not None:
            x, y, w, h = component['bounds']
            if w <= 0 or h <= 0 or x+w > doc['source']['width_px'] or y+h > doc['source']['height_px']:
                errors.append(f'component {component["id"]} bounds outside reference image')
    for asset in doc['assets']:
        if asset.get('file'):
            asset_path = PurePosixPath(asset['file'].replace('\\', '/'))
            if asset_path.is_absolute() or '..' in asset_path.parts:
                errors.append(f'asset {asset["id"]} has unsafe file path')
        if asset['status'] in ('original-used', 'extracted', 'generated-with-authorization') and not asset['file']:
            errors.append(f'asset {asset["id"]} marked present without file')
    return errors


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('spec', type=Path)
    p.add_argument('--schema', type=Path, default=ROOT / 'references/design-spec.schema.json')
    a = p.parse_args()
    schema = json.loads(a.schema.read_text(encoding='utf-8'))
    doc = json.loads(a.spec.read_text(encoding='utf-8'))
    errors = validate(doc, schema)
    if errors:
        print('FAILED design-spec:')
        for err in errors: print(' - ' + err)
        raise SystemExit(1)
    print('PASS design-spec schema, region bounds, IDs, and asset references')


if __name__ == '__main__':
    main()
