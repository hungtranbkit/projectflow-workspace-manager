#!/usr/bin/env python3
"""Benchmark verifier: distinguishes real references from synthetic fixtures.
Only accepted as independent evidence with documented origin and real browser capture.
"""
from pathlib import Path
import argparse,json

def summarize(manifest):
 projects=manifest.get('projects',[])
 if not projects:raise ValueError('projects required')
 observed=[];negative=[]
 for project in projects:
  if not project.get('name') or 'states' not in project:raise ValueError('Projects require name and states')
  independent=bool(project.get('approved_by_owner') and project.get('real_project') and project.get('independent_references'))
  expected={'mobile','tablet','desktop'}
  refs={s.get('viewport') for s in project['states'] if s.get('reference_independent') and s.get('browser_render') and s.get('visually_reviewed')}
  complete=independent and expected.issubset(refs)
  observed.append({'name':project['name'],'independent':independent,'required_reference_coverage':complete,'covered':sorted(x for x in refs if x),
                   'approved_real_project_benchmark':complete})
  for control in project.get('negative_controls',[]):
   if 'should_fail' not in control or 'gate_failed' not in control:raise ValueError('Negative controls need expected and actual result')
   negative.append({'project':project['name'],**control})
 false_positives=sum(1 for x in negative if x['should_fail'] and not x['gate_failed'])
 true_negatives=sum(1 for x in negative if x['should_fail'] and x['gate_failed'])
 denominator=false_positives+true_negatives
 return {'projects':observed,'approved_project_count':sum(x['approved_real_project_benchmark'] for x in observed),
         'independent_benchmark_passed':sum(x['approved_real_project_benchmark'] for x in observed)>=3,
         'negative_controls':{'total':denominator,'caught':true_negatives,'false_positives':false_positives,'false_positive_rate':false_positives/denominator if denominator else None},
         'note':'Fixtures and reference screenshots without independent mobile/tablet/desktop approval do not satisfy P0.'}

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
 a=p.parse_args();out=summarize(json.loads(a.manifest.read_text(encoding='utf-8')));a.out.parent.mkdir(parents=True,exist_ok=True)
 a.out.write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8');print(json.dumps(out,indent=2))