#!/usr/bin/env python3
"""Collect independent state QA folders into a linked Markdown and HTML dashboard."""
import argparse,json,html
from pathlib import Path

def build(folders,out):
    if len(folders)<1:raise ValueError('At least one state folder required')
    out.mkdir(parents=True,exist_ok=True)
    rows=[]
    for f in folders:
        stats=json.loads((f/'qa.json').read_text(encoding='utf-8'))
        rows.append({'name':f.name,'folder':f.resolve().as_uri(),'metric':stats['overall'],
                     'masked_fraction':stats['masked_fraction'], 'overlay':(f/'overlay.html').resolve().as_uri(),
                     'heatmap':(f/'difference-masked.png').resolve().as_uri(),
                     'json':(f/'qa.json').resolve().as_uri()})
    markdown=['# Image to Code — state review dashboard','','| State | RGB MAE | SSIM | Masked | Review |','|---|---:|---:|---:|---|']
    for r in rows:markdown.append(f"| {r['name']} | {r['metric']['rgb_mae']} | {r['metric'].get('masked_ssim')} | {r['masked_fraction']:.1%} | [Overlay]({r['overlay']}) · [Heatmap]({r['heatmap']}) · [JSON]({r['json']}) |")
    markdown+=['','Measurements are diagnostic. Review every state in-browser and never upload private screenshots as public CI artifacts.']
    (out/'dashboard.md').write_text('\n'.join(markdown)+'\n',encoding='utf-8')
    cards=''.join(f'<article><h2>{html.escape(x["name"])}</h2><p>MAE: {x["metric"]["rgb_mae"]} · SSIM: {x["metric"].get("masked_ssim")}</p><a href="{html.escape(x["overlay"])}">Compare</a> · <a href="{html.escape(x["heatmap"])}">Heatmap</a></article>' for x in rows)
    (out/'dashboard.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Image-to-Code QA</title><style>body{font:16px system-ui;margin:40px auto;max-width:850px;padding:0 20px}article{border-bottom:1px solid #ccc;padding:20px 0}a{color:inherit}</style><h1>Visual state QA</h1>'+cards+'<p>Local files, no uploaded reference images.</p></html>',encoding='utf-8')
    return rows
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--state-dir',type=Path,action='append',required=True);p.add_argument('--out',type=Path,required=True)
 a=p.parse_args();build(a.state_dir,a.out)