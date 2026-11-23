#!/usr/bin/env python3
"""Collect sanitised QA counters and longitudinal GitHub Actions history.

Never uploads images, repository secrets, raw logs or font binaries. Missing past
artifacts are UNKNOWN, not zeros. GitHub token is used only in a request header.
"""
import argparse
from datetime import datetime,timezone
from io import BytesIO
from pathlib import Path
import csv
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from zipfile import ZipFile

METRIC_FIELDS=('python_total','python_passed','js_total','js_passed','browser_tests','browser_passed','negative_controls','negative_passed')


def clamp_int(value):
    if value is None:return None
    if type(value) is bool or not isinstance(value,int) or value<0:raise ValueError('Expected nonnegative integer or null')
    return value


def validate(record):
    result={k:record.get(k) for k in ('schema_version','run_id','run_attempt','head_sha','created_at','source_job','status','conclusion')}
    if result['schema_version']!=1:raise ValueError('Unsupported schema')
    for k in METRIC_FIELDS:result[k]=clamp_int(record.get(k))
    for t,p in [('python_total','python_passed'),('js_total','js_passed'),('browser_tests','browser_passed'),('negative_controls','negative_passed')]:
        if result[t] is not None and result[p] is not None and result[p]>result[t]:raise ValueError(f'{p} > {t}')
    if result['head_sha'] and not re.fullmatch(r'[0-9a-f]{40}',result['head_sha']):raise ValueError('Invalid head SHA')
    return result


def make_record(*,source_job='tests',run_id=None,sha=None,python_total=None,python_passed=None,js_total=None,js_passed=None,browser_tests=None,browser_passed=None,negative_controls=None,negative_passed=None,conclusion=None):
    record={'schema_version':1,'run_id':run_id,'run_attempt':None,'head_sha':sha,'created_at':datetime.now(timezone.utc).isoformat(),'source_job':source_job,'status':'completed' if conclusion else 'unverified','conclusion':conclusion, 'python_total':python_total,'python_passed':python_passed,'js_total':js_total,'js_passed':js_passed,'browser_tests':browser_tests,'browser_passed':browser_passed,'negative_controls':negative_controls,'negative_passed':negative_passed}
    return validate(record)


def collect_from_log(log,source_job='tests',run_id=None,sha=None):
    python_matches=re.findall(r'Ran\s+(\d+)\s+tests?',log)
    node_matches=re.findall(r'^#\s+tests\s+(\d+)\s*$',log,re.M)
    passed_matches=re.findall(r'^#\s+pass\s+(\d+)\s*$',log,re.M)
    py_total=int(python_matches[-1]) if python_matches else None
    js_total=int(node_matches[-1]) if node_matches else None
    js_passed=int(passed_matches[-1]) if passed_matches else None
    py_passed=py_total if py_total is not None and re.search(r'^OK\s*$',log,re.M) else None
    return make_record(source_job=source_job,run_id=run_id,sha=sha,python_total=py_total,python_passed=py_passed,js_total=js_total,js_passed=js_passed)


def github_json(url,token):
    req=urllib.request.Request(url,headers={'Accept':'application/vnd.github+json','Authorization':f'Bearer {token}','X-GitHub-Api-Version':'2022-11-28','User-Agent':'image-to-code-qa-metrics'})
    with urllib.request.urlopen(req,timeout=20) as res:return json.loads(res.read())


def github_bytes(url,token):
    req=urllib.request.Request(url,headers={'Accept':'application/vnd.github+json','Authorization':f'Bearer {token}','X-GitHub-Api-Version':'2022-11-28','User-Agent':'image-to-code-qa-metrics'})
    with urllib.request.urlopen(req,timeout=20) as res:return res.read()


def read_artifact_payload(b):
    with ZipFile(BytesIO(b)) as z:
        candidates=[n for n in z.namelist() if Path(n).name.startswith('qa-metrics') and Path(n).suffix=='.json' and not n.startswith('/') and '..' not in Path(n).parts]
        if not candidates:return []
        records=[]
        for name in candidates[:20]:
            data=z.read(name)
            if len(data)>100000:continue
            try:
                doc=json.loads(data)
                payload=doc if isinstance(doc,list) else [doc]
                records.extend(validate(x) for x in payload)
            except (ValueError,TypeError,json.JSONDecodeError):continue
        return records


def combine(run,artifact_records):
    # One row per workflow run; prefer metrics of each verified job, use UNKNOWN for absent jobs.
    result={'run_id':run['id'],'run_attempt':run.get('run_attempt',1),'head_sha':run.get('head_sha'), 'created_at':run.get('created_at'),'status':run.get('status'), 'conclusion':run.get('conclusion'),'url':run.get('html_url'), 'metrics_available':bool(artifact_records)}
    for k in METRIC_FIELDS:result[k]=None
    for item in artifact_records:
        for k in METRIC_FIELDS:
            if item[k] is not None:
                # do not count duplicate entries from rerun of same job twice
                result[k]=max(result[k] or 0,item[k])
    result['coverage']='VERIFIED_METRICS' if result['metrics_available'] else 'NO_ARTIFACT_OR_EXPIRED'
    return result


def aggregate(runs,record_map):
    rows=[combine(run,record_map.get(run['id'],[])) for run in runs]
    rows.sort(key=lambda x:x['created_at'] or '',reverse=True)
    complete=sum(x['conclusion']=='success' for x in rows)
    known=sum(x['metrics_available'] for x in rows)
    return {'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),'runs_total':len(rows),'runs_success':complete,'runs_with_metrics':known,'artifact_coverage':known/len(rows) if rows else None,'runs':rows,
        'note':'Historical runs without a qa-metrics artifact remain unknown; pass rates require sufficient verified coverage.'}


def gh_download_records(repo, token, run_id, artifact_name):
    """Fallback to official GitHub CLI on Actions when HTTP archive redirect fails.

    All arguments are passed as a vector; the token is in the environment, not
    command arguments or logs. A failed download never becomes an empty PASS.
    """
    gh=shutil.which('gh')
    if not gh:return [], 'GH_CLI_UNAVAILABLE'
    with tempfile.TemporaryDirectory(prefix='i2c-qa-') as temp:
        env={**os.environ,'GH_TOKEN':token}
        try:
            proc=subprocess.run([gh,'run','download',str(run_id),'-R',repo,'-n',artifact_name,'-D',temp],env=env,capture_output=True,text=True,timeout=35,check=False)
            if proc.returncode!=0:return [], 'GH_DOWNLOAD_FAILED'
        except subprocess.TimeoutExpired:return [], 'GH_DOWNLOAD_TIMEOUT'
        except OSError:return [], 'GH_DOWNLOAD_OS_ERROR'
        results=[]
        for filename in Path(temp).rglob('qa-metrics*.json'):
            if not filename.is_file() or filename.stat().st_size>100000:continue
            try:
                doc=json.loads(filename.read_text())
                for rec in doc if isinstance(doc,list) else [doc]:results.append(validate(rec))
            except (ValueError,TypeError,json.JSONDecodeError):continue
        return results, 'GH_DOWNLOAD_SUCCEEDED' if results else 'GH_EMPTY_OR_INVALID'


def download_history(repo,token,limit=30):
    from urllib.parse import quote
    owner,name=repo.split('/',1)
    url=f'https://api.github.com/repos/{quote(owner)}/{quote(name)}/actions/workflows/ci.yml/runs?per_page={min(limit,100)}&exclude_pull_requests=true'
    run_data=github_json(url,token)
    runs=run_data.get('workflow_runs',[])[:limit]
    records={}
    diagnostics={}
    for run in runs:
        artifact_api=f'https://api.github.com/repos/{quote(owner)}/{quote(name)}/actions/runs/{run["id"]}/artifacts?per_page=100'
        try:artifacts=github_json(artifact_api,token).get('artifacts',[])
        except urllib.error.HTTPError as e:
            diagnostics[run['id']] = f'ARTIFACT_LIST_HTTP_{e.code}'
            continue
        wanted=[o for o in artifacts if not o.get('expired') and o.get('name','').startswith('qa-metrics-')]
        if not wanted:
            diagnostics[run['id']] = 'ARTIFACT_NOT_PUBLISHED_OR_EXPIRED'
        for obj in wanted:
            try:
                blob=github_bytes(obj['archive_download_url'],token)
                parsed=read_artifact_payload(blob)
                if parsed:
                    records.setdefault(run['id'],[]).extend(parsed)
                    continue
                err='DIRECT_ZIP_EMPTY'
            except urllib.error.HTTPError as e:err=f'DIRECT_DOWNLOAD_HTTP_{e.code}'
            except (ValueError,KeyError):err='DIRECT_DOWNLOAD_INVALID'
            parsed,method=gh_download_records(repo,token,run['id'],obj['name'])
            if parsed:records.setdefault(run['id'],[]).extend(parsed)
            else:diagnostics[run['id']] = f'{err}; {method}'
    report=aggregate(runs,records)
    for row in report['runs']:
        row['collection_note']=diagnostics.get(row['run_id'],'OK' if row['metrics_available'] else 'UNKNOWN')
    return report


def write_outputs(report,out):
    out.mkdir(parents=True,exist_ok=True)
    (out/'history.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    cols=['run_id','created_at','head_sha','conclusion','coverage',*METRIC_FIELDS]
    with (out/'history.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(report['runs'])
    lines=['# Image to Code — Histórico de Qualidade','',f'Execuções analisadas: **{report["runs_total"]}**; métricas disponíveis: **{report["runs_with_metrics"]}**.','', '|Execução|Resultado|Cobertura|Python|JavaScript|Navegador|', '|---|---|---|---:|---:|---:|']
    for row in report['runs']:
        def fraction(a,b):return f'{row[b]}/{row[a]}' if row[a] is not None and row[b] is not None else 'N/D'
        lines.append('|{id}|{conc}|{cov}|{py}|{js}|{br}|'.format(id=row['run_id'],conc=row['conclusion'] or 'em andamento',cov=row['coverage'],py=fraction('python_total','python_passed'),js=fraction('js_total','js_passed'),br=fraction('browser_tests','browser_passed')))
    lines+=['','Ausência de artefato é **N/D**, não zero. Métricas são diagnósticas; não provam fidelidade 10/10.']
    (out/'history.md').write_text('\n'.join(lines)+'\n')
    trs=''.join(f'<tr><td>{html.escape(str(x["run_id"]))}</td><td>{html.escape(str(x["created_at"]))}</td><td>{html.escape(str(x["conclusion"]))}</td><td>{html.escape(x["coverage"])}</td><td>{x["python_passed"] if x["python_passed"] is not None else "N/D"}</td></tr>' for x in report['runs'])
    (out/'history.html').write_text('<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Image to Code — CI Metrics</title><style>body{font:16px system-ui;margin:3rem auto;max-width:960px;color:#16191d}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #ccc;text-align:left}th{background:#eee}</style><h1>Image to Code — Histórico de QA</h1><p>Valores ausentes não são contados como falhas.</p><table><thead><tr><th>Execução</th><th>Data</th><th>Conclusão</th><th>Cobertura</th><th>Python aprovados</th></tr></thead><tbody>'+trs+'</tbody></table></html>')
    return [str(out/x) for x in ('history.json','history.csv','history.md','history.html')]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='mode',required=True)
    collect=sub.add_parser('collect');collect.add_argument('--log',type=Path,required=True);collect.add_argument('--job',default='tests');collect.add_argument('--out',type=Path,required=True)
    record=sub.add_parser('record');record.add_argument('--job',required=True);record.add_argument('--out',type=Path,required=True);record.add_argument('--browser-tests',type=int);record.add_argument('--browser-passed',type=int);record.add_argument('--negative-controls',type=int);record.add_argument('--negative-passed',type=int)
    ag=sub.add_parser('aggregate');ag.add_argument('--input',type=Path,help='offline test input object {runs:[], records:{id:[]}}');ag.add_argument('--repo');ag.add_argument('--out',type=Path,required=True);ag.add_argument('--limit',type=int,default=30)
    a=p.parse_args()
    if a.mode=='collect':
        rec=collect_from_log(a.log.read_text(),a.job,os.getenv('GITHUB_RUN_ID'),os.getenv('GITHUB_SHA'))
        a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(rec,indent=2)+'\n');print(a.out)
    elif a.mode=='record':
        rec=make_record(source_job=a.job,run_id=os.getenv('GITHUB_RUN_ID'),sha=os.getenv('GITHUB_SHA'),browser_tests=a.browser_tests,browser_passed=a.browser_passed,negative_controls=a.negative_controls,negative_passed=a.negative_passed,conclusion='success')
        a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(rec,indent=2)+'\n');print(a.out)
    else:
        if a.input:
            data=json.loads(a.input.read_text());result=aggregate(data['runs'],{int(k):v for k,v in data.get('records',{}).items()})
        else:
            if not a.repo or not os.getenv('GITHUB_TOKEN'):raise SystemExit('A repo and GITHUB_TOKEN are required for live history collection')
            result=download_history(a.repo,os.environ['GITHUB_TOKEN'],a.limit)
        files=write_outputs(result,a.out);print(json.dumps({'run_count':result['runs_total'],'with_metrics':result['runs_with_metrics'],'outputs':files}))

if __name__=='__main__':main()