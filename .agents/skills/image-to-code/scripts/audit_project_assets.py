#!/usr/bin/env python3
"""Verify static image references against a web project's public files.

Complements browser checks: onError fallback may make a missing original image invisible
in the final DOM, while source code still references an absent file. No network, no
modification of application assets.
"""
import argparse
import json
from pathlib import Path
import re

IMAGE_EXT = r'(?:png|jpe?g|webp|avif|gif|svg)'
# Static quoted paths only. Deliberately do not try to resolve dynamic import expressions.
PATTERN = re.compile(r'["\'`](?P<path>/(?:[^\s"\'`?]+/)*[^\s"\'`?]+\.' + IMAGE_EXT + r')(?:\?[^"\'`]*)?["\'`]', re.IGNORECASE)
SOURCE_EXT = {'.js', '.jsx', '.ts', '.tsx', '.css', '.scss', '.html', '.vue', '.svelte'}
EXCLUDED = {'node_modules', '.git', 'dist', 'build', 'coverage', '.next', '.vercel', 'artifacts'}


def audit(project: Path, source_dirs=('src',), public_dir='public'):
    project = project.resolve()
    public = (project / public_dir).resolve()
    if not public.is_relative_to(project):
        raise ValueError('Public directory must remain inside project')
    refs = {}
    scanned = 0
    for dirname in source_dirs:
        base = (project / dirname).resolve()
        if not base.is_relative_to(project):
            raise ValueError('Source directory must remain inside project')
        if not base.exists():
            continue
        for file in base.rglob('*'):
            if not file.is_file() or file.suffix.lower() not in SOURCE_EXT or any(seg in EXCLUDED for seg in file.relative_to(base).parts):
                continue
            scanned += 1
            source = file.read_text('utf-8', errors='replace')
            for hit in PATTERN.finditer(source):
                resource = hit.group('path')
                # Pure public URL: URLs are slash-prefixed; path traversal cannot escape public.
                relative = resource.lstrip('/')
                target = (public / relative).resolve()
                if not target.is_relative_to(public):
                    status = 'unsafe'
                elif target.is_file():
                    status = 'present'
                else:
                    status = 'missing'
                line = source.count('\n', 0, hit.start()) + 1
                key = (resource, str(file.relative_to(project)), line)
                refs[key] = {'url': resource, 'source': str(file.relative_to(project)), 'line': line, 'status': status}
    items = sorted(refs.values(), key=lambda i: (i['source'], i['line'], i['url']))
    missing = [i for i in items if i['status'] != 'present']
    return {'project': str(project), 'source_files_scanned': scanned,
            'public_path': str(public), 'references': items,
            'missing': missing, 'passed': len(missing) == 0,
            'caveat': 'Static literal image URLs only; dynamic path expressions and external CDNs require separate review.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path('.'))
    parser.add_argument('--source-dir', action='append', default=None)
    parser.add_argument('--public-dir', default='public')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--allow-missing', action='store_true')
    args = parser.parse_args()
    result = audit(args.project, args.source_dir or ['src'], args.public_dir)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + '\n', encoding='utf-8')
    print(text)
    if not result['passed'] and not args.allow_missing:
        raise SystemExit(1)

if __name__ == '__main__':
    main()