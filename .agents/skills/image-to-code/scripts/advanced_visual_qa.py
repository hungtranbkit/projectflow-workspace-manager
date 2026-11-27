#!/usr/bin/env python3
"""Optional fine-grained visual QA. Metrics are diagnostics, never quality scores.

No image registration, masks or suggested CSS are applied to the source or rendered image.
Mask rules are enforced before any metric is reported.
"""
from __future__ import annotations
import argparse, json, math, base64, io
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFilter
import numpy as np


def read_box(box, size):
    if not isinstance(box, list) or len(box) != 4 or not all(isinstance(x,(int,float)) for x in box):
        raise ValueError('Box must be [left,top,right,bottom]')
    left,top,right,bottom=map(int,box)
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise ValueError(f'Invalid or out-of-bounds box: {box} for {size}')
    return left,top,right,bottom


def build_mask(size, boxes, ceiling=.08, critical=None):
    if not 0 <= ceiling <= .08: raise ValueError('Mask limit may not exceed 8%')
    mask=Image.new('L',size,255);draw=ImageDraw.Draw(mask)
    blocked=[read_box(v,size) for v in (critical or [])]
    for item in boxes:
        if not isinstance(item,dict) or not item.get('reason') or 'box' not in item:
            raise ValueError('Each mask requires box and an explicit reason')
        rect=read_box(item['box'],size)
        if any(rect[0]<b[2] and rect[2]>b[0] and rect[1]<b[3] and rect[3]>b[1] for b in blocked):
            raise ValueError('Cannot mask a critical face/CTA/icon region')
        draw.rectangle((rect[0],rect[1],rect[2]-1,rect[3]-1),fill=0)
    valid=np.asarray(mask)>0
    masked=1-valid.mean()
    if masked>ceiling+1e-8:raise ValueError(f'Mask exceeds ceiling: {masked:.2%} > {ceiling:.2%}')
    return valid,round(float(masked),6)


def edge_alignment_hint(ref,render,max_shift=16):
    """Bounded edge correlation reports an advisory translation, never transforms images."""
    if ref.size != render.size:raise ValueError('Matching dimensions required')
    def ed(im):
        gray=np.asarray(im.convert('L').resize((max(16,im.width//2),max(16,im.height//2))))
        gy,gx=np.gradient(gray.astype(float))
        norm=np.hypot(gx,gy)
        return norm/(np.linalg.norm(norm)+1e-9)
    a,b=ed(ref),ed(render)
    bound=min(max_shift//2,12); best=(-float('inf'),0,0)
    for dy in range(-bound,bound+1):
        for dx in range(-bound,bound+1):
            x1=max(0,dx);x2=min(a.shape[1],a.shape[1]+dx)
            y1=max(0,dy);y2=min(a.shape[0],a.shape[0]+dy)
            if x2<=x1 or y2<=y1:continue
            val=float(np.sum(a[y1:y2,x1:x2]*b[y1-dy:y2-dy,x1-dx:x2-dx]))
            if val>best[0]:best=(val,dx*2,dy*2)
    return {'suggested_render_shift_px':[best[1],best[2]],'edge_correlation':round(best[0],5),
            'applied':False,'note':'Advisory only; original pixels were not moved or warped.'}


def score_region(ref,render,valid):
    a=np.asarray(ref.convert('RGB'),dtype=np.float32); b=np.asarray(render.convert('RGB'),dtype=np.float32)
    if not valid.any():raise ValueError('No unmasked pixels')
    error=np.abs(a-b);d=error.max(axis=2)
    out={'rgb_mae':round(float(error[valid].mean()),3),
         'mismatch_fraction_gt20':round(float((d[valid]>20).mean()),6),
         'visible_pixels':int(valid.sum())}
    try:
        from skimage.metrics import structural_similarity as ssim
        if min(a.shape[:2])>=7:
            _,ssim_map=ssim(a.astype(np.uint8),b.astype(np.uint8),data_range=255,channel_axis=2,full=True)
            if ssim_map.ndim==3:ssim_map=ssim_map.mean(axis=2)
            # Reject pixels near masks, whose SSIM convolution depends on masked neighbors.
            mask_image=Image.fromarray(valid.astype('uint8')*255)
            safe=np.asarray(mask_image.filter(ImageFilter.MinFilter(7)))==255
            if safe.any():out['masked_ssim']=round(float(ssim_map[safe].mean()),5)
            else:out['masked_ssim']=None
    except ImportError:out['masked_ssim']=None
    return out


def render_overlay(reference,render,path):
    a=io.BytesIO();b=io.BytesIO()
    reference.save(a,format='PNG');render.save(b,format='PNG')
    url_a='data:image/png;base64,'+base64.b64encode(a.getvalue()).decode()
    url_b='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
    html='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Image to Code — visual overlay</title><style>*{box-sizing:border-box}body{margin:0;background:#171717;color:#fafafa;font:15px system-ui;padding:24px}main{max-width:1200px;margin:auto}.comparison{position:relative;max-width:100%;width:fit-content;line-height:0;box-shadow:0 10px 40px #0007}.comparison img{display:block;max-width:100%;height:auto}.render{position:absolute;inset:0;overflow:hidden}.render img{max-width:none}.toolbar{display:flex;align-items:center;gap:14px;margin:15px 0 25px}input{width:min(420px,65vw)}.line{position:absolute;inset:0 auto 0 50%;width:2px;background:#ffda5b;pointer-events:none}p{color:#b8b8b8}</style><main><h1>Original / rendered browser capture</h1><p>Drag the slider to inspect differences. Reference image data is embedded locally; nothing is uploaded or changed.</p><div class="toolbar"><label for="split">Split</label><input id="split" aria-label="Overlay split" type="range" min="0" max="100" value="50"><output id="value">50%</output></div><div class="comparison" id="c"><img id="a" src="REFA" alt="Reference"><div class="render" id="r"><img id="b" src="RENB" alt="Browser capture"></div><div class="line" id="l"></div></div><script>const s=document.querySelector('#split'),r=document.querySelector('#r'),l=document.querySelector('#l'),v=document.querySelector('#value'),a=document.querySelector('#a'),b=document.querySelector('#b');function update(){r.style.clipPath=`inset(0 ${100-Number(s.value)}% 0 0)`;l.style.left=s.value+'%';v.textContent=s.value+'%';b.style.width=a.getBoundingClientRect().width+'px'}s.oninput=update;window.addEventListener('resize',update);a.onload=update;update();</script></main></html>'''
    path.write_text(html.replace('REFA',url_a).replace('RENB',url_b),encoding='utf-8')


def run(config,out):
    ref=Image.open(config['reference']).convert('RGB');render=Image.open(config['render']).convert('RGB')
    if ref.size!=render.size:raise ValueError('Reference and render must have matching dimensions; normalize explicitly elsewhere')
    out.mkdir(parents=True,exist_ok=True)
    critical=[x['box'] for x in config.get('regions',[]) if x.get('priority')=='critical']
    valid,fraction=build_mask(ref.size,config.get('masks',[]),float(config.get('max_mask_fraction',.08)),critical)
    scores={'source':{'reference':str(config['reference']),'render':str(config['render'])},'size':list(ref.size),
            'masked_fraction':fraction,'overall':score_region(ref,render,valid),
            'registration_hint':edge_alignment_hint(ref,render), 'regions':[]}
    priorities={'critical':1,'high':2,'normal':3}
    for region in config.get('regions',[]):
        left,top,right,bottom=read_box(region['box'],ref.size)
        mask=valid[top:bottom,left:right]
        part=score_region(ref.crop((left,top,right,bottom)),render.crop((left,top,right,bottom)),mask)
        scores['regions'].append({'name':region['name'],'priority':region.get('priority','normal'), 'box':[left,top,right,bottom],**part})
    diff=ImageChops.difference(ref,render)
    heat=ImageChops.lighter(ImageChops.lighter(diff.getchannel('R'),diff.getchannel('G')),diff.getchannel('B'))
    rgb=Image.merge('RGB',(heat,heat,heat))
    masked=Image.new('RGB',ref.size,'#777777')
    m=Image.fromarray(valid.astype('uint8')*255)
    rgb=Image.composite(rgb,masked,m)
    rgb.save(out/'difference-masked.png')
    render_overlay(ref,render,out/'overlay.html')
    proposals=[]
    for item in config.get('components',[]):
        if not item.get('selector') or 'reference' not in item or 'actual' not in item:continue
        rr=item['reference'];aa=item['actual']
        delta={k:round(rr[k]-aa[k],2) for k in ('x','y','width','height') if k in rr and k in aa}
        tol=item.get('tolerance_px',2)
        severity='critical' if item.get('priority')=='critical' else 'normal'
        proposals.append({'selector':item['selector'],'priority':severity,'delta_px':delta,'exceeds_tolerance':any(abs(x)>tol for x in delta.values()),
                          'review_only':True,'suggestion':f"/* REVIEW ONLY: {item['selector']} measured Δ={delta}; verify parent/layout constraints before adjusting */"})
    scores['css_proposals']=proposals
    (out/'qa.json').write_text(json.dumps(scores,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    lines=['# Visual fidelity review','',f"Reference: `{config['reference']}`",f"Render: `{config['render']}`",'',
           f"Masked fraction: **{fraction:.2%}** (ceiling {config.get('max_mask_fraction',.08):.0%}).",'',
           '| Region | Priority | Mask-aware SSIM | RGB MAE |','|---|---|---:|---:|']
    for x in scores['regions']:lines.append(f"| {x['name']} | {x['priority']} | {x.get('masked_ssim')} | {x['rgb_mae']} |")
    lines+=['','[Local overlay inspector](overlay.html) · [Diagnostic heatmap](difference-masked.png) · [Machine data](qa.json)','',
            'No image was shifted, warped or uploaded. Region metrics and suggested CSS are diagnostics, not approval.']
    (out/'dashboard.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return scores

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'))
    print(json.dumps(run(config,args.out),indent=2,ensure_ascii=False))