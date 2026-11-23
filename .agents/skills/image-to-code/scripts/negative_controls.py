#!/usr/bin/env python3
"""Execute (not self-report) expected-failing controls for QA false-positive calibration."""
import json,argparse
from pathlib import Path
from advanced_visual_qa import build_mask
from validate_component_geometry import check


def cases():
 def oversized_mask():build_mask((100,100),[{'box':[0,0,40,40],'reason':'dynamic'}])
 def critical_mask():build_mask((100,100),[{'box':[0,0,15,15],'reason':'dynamic'}],critical=[[5,5,20,20]])
 def clipped():
  result=check({'components':[{'selector':'#phone','priority':'critical','clipped':True,'reference':{'x':0,'y':0,'width':90,'height':200},'actual':{'x':0,'y':0,'width':90,'height':200}}]})
  if not result['passed']:raise ValueError('Clipped UI rejected')
 def displaced():
  result=check({'components':[{'selector':'#cta','priority':'critical','reference':{'x':0,'y':0,'width':90,'height':40},'actual':{'x':20,'y':0,'width':90,'height':40}}]})
  if not result['passed']:raise ValueError('CTA geometry rejected')
 return {'oversized_mask':oversized_mask,'critical_region_mask':critical_mask,'clipped_phone':clipped,'displaced_cta':displaced}

def execute():
 checks=[]
 for name,fn in cases().items():
  try:fn();failed=False;detail='unexpectedly accepted'
  except (ValueError,AssertionError) as exc:failed=True;detail=str(exc)
  checks.append({'name':name,'should_fail':True,'gate_failed':failed,'detail':detail})
 return {'controls':checks,'caught':sum(x['gate_failed'] for x in checks),'total':len(checks),
         'false_positive_rate':sum(not x['gate_failed'] for x in checks)/len(checks),
         'scope':'synthetic negative controls only; not field false-positive rate'}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();r=execute()
 a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r,indent=2))
 if r['caught']!=r['total']:raise SystemExit(1)