#!/usr/bin/env python3
"""Semantic priority component gate. Never waive clipping for a high score."""
import argparse,json
from pathlib import Path

DEFAULTS={'critical':3,'high':5,'normal':9,'decorative':14}

def check(doc):
    results=[]
    for item in doc.get('components',[]):
        label=item['selector'];a=item['reference'];b=item['actual'];priority=item.get('priority','normal')
        if priority not in DEFAULTS:raise ValueError('Unknown priority '+priority)
        tol=float(item.get('tolerance_px',DEFAULTS[priority]))
        if tol<0 or tol>30:raise ValueError('Tolerance must be 0..30px')
        delta={k:round(float(b[k])-float(a[k]),2) for k in ('x','y','width','height')}
        # a clipped or zero-dimension critical node always fails, regardless of tolerance.
        clipped=bool(item.get('clipped') or b['width']<=0 or b['height']<=0 or item.get('hidden'))
        passed=not clipped and max(abs(x) for x in delta.values())<=tol
        results.append({'selector':label,'priority':priority,'tolerance_px':tol,'delta':delta,
                        'clipped_or_hidden':clipped,'passed':passed})
    return {'passed':bool(results) and all(x['passed'] for x in results),'components':results,
            'note':'Geometry evidence must be derived from actual browser DOM rectangles. No auto CSS edits.'}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
 a=p.parse_args();r=check(json.loads(a.config.read_text(encoding='utf-8')))
 a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r,indent=2))
 if not r['passed']:raise SystemExit(1)