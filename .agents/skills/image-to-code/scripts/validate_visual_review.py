#!/usr/bin/env python3
"""Validate required independent visual-review evidence without inventing scores.

Exit 1 on missing evidence, unknown verdicts or failures. Does not infer visual correctness.
"""
import argparse
import json
import sys
from pathlib import Path

REQUIRED = ('icons', 'typography_controls', 'subject_integrity', 'lighting_composition')
VALID = ('pass', 'fail', 'unknown')


def check(data, root: Path):
    problems = []
    reviews = data.get('reviews', {})
    if not isinstance(reviews, dict):
        return ['reviews must be an object']
    for name in REQUIRED:
        item = reviews.get(name)
        if not isinstance(item, dict):
            problems.append(f'{name}: review missing')
            continue
        status = item.get('status')
        if status not in VALID:
            problems.append(f'{name}: invalid status; use pass, fail or unknown')
        elif status != 'pass':
            problems.append(f'{name}: status={status} blocks approval')
        if not isinstance(item.get('notes'), str) or len(item['notes'].strip()) < 16:
            problems.append(f'{name}: substantive notes missing')
        evidence = item.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            problems.append(f'{name}: file evidence missing')
            continue
        for entry in evidence:
            if not isinstance(entry, str) or not entry:
                problems.append(f'{name}: invalid evidence path')
                continue
            path = Path(entry)
            if path.is_absolute() or '..' in path.parts:
                problems.append(f'{name}: unsafe evidence path {entry}')
                continue
            target = (root / path).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                problems.append(f'{name}: path escapes workspace {entry}')
                continue
            if not target.is_file():
                problems.append(f'{name}: evidence not found {entry}')
    return problems


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--file', required=True)
    p.add_argument('--root', required=True)
    a = p.parse_args()
    try:
        errors = check(json.loads(Path(a.file).read_text(encoding='utf-8')), Path(a.root))
    except (ValueError, OSError) as ex:
        errors = [str(ex)]
    print(json.dumps({'approved': not errors, 'required_reviews': list(REQUIRED), 'issues': errors}, ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
