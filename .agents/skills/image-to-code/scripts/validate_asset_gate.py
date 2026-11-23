#!/usr/bin/env python3
"""Block image-to-code implementation until required visual assets are accounted for.

Usage: python scripts/validate_asset_gate.py --manifest artifacts/assets-ready.json --root /path/to/project
"""
import argparse, hashlib, json, sys
from pathlib import Path


def validate(manifest: dict, root: Path):
    failures=[]
    if manifest.get('stage')!='assets_before_code': failures.append('stage must be assets_before_code')
    roles=set(manifest.get('required_roles', []))
    assets=manifest.get('assets', [])
    by_role={}
    for item in assets:
        role=item.get('role')
        by_role.setdefault(role,[]).append(item)
        status=item.get('status')
        if status not in ('extracted','reused','reconstructed','unrecoverable'):
            failures.append(f'{role}: invalid status')
            continue
        if status=='unrecoverable':
            if not item.get('reason'): failures.append(f'{role}: unrecoverable without reason')
            continue
        pathstr=item.get('path','')
        p=Path(pathstr)
        if not pathstr or p.is_absolute() or '..' in p.parts:
            failures.append(f'{role}: unsafe or missing relative path')
            continue
        resolved=(root/p).resolve()
        try: resolved.relative_to(root.resolve())
        except ValueError:
            failures.append(f'{role}: path escapes root')
            continue
        if not resolved.is_file():
            failures.append(f'{role}: missing file {pathstr}')
            continue
        actual=hashlib.sha256(resolved.read_bytes()).hexdigest()
        if item.get('sha256')!=actual: failures.append(f'{role}: SHA-256 mismatch {pathstr}')
        if not item.get('provenance'): failures.append(f'{role}: missing provenance')
    for role in roles:
        found=by_role.get(role,[])
        if not found: failures.append(f'{role}: no asset entry')
        if any(i.get('status')=='unrecoverable' for i in found):
            failures.append(f'{role}: unrecoverable; must be disclosed and explicitly handled before implementation')
    if not roles: failures.append('required_roles must not be empty')
    return failures


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--manifest',required=True)
    ap.add_argument('--root',required=True)
    args=ap.parse_args()
    m=json.loads(Path(args.manifest).read_text())
    err=validate(m,Path(args.root))
    print(json.dumps({'passed':not err,'asset_count':len(m.get('assets',[])),'roles':m.get('required_roles',[]),'failures':err},ensure_ascii=False,indent=2))
    sys.exit(1 if err else 0)
if __name__=='__main__': main()
