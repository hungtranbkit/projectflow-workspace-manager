#!/usr/bin/env python3
"""Font metadata + rendered icon shape comparison, when original licensed assets exist.
No fonts are embedded in distributed reports, no source icon path is modified.
"""
import argparse,json,io
from pathlib import Path
from PIL import Image
import numpy as np


def font_metadata(file):
    from fontTools.ttLib import TTFont
    font=TTFont(file,lazy=True)
    names=font['name']
    def label(which):
        vals=[]
        for record in names.names:
            if record.nameID==which:
                try: val=record.toUnicode()
                except UnicodeDecodeError:continue
                if val not in vals:vals.append(val)
        return vals[:6]
    result={'family':label(1),'subfamily':label(2),'postscript':label(6),
            'weight_class':font['OS/2'].usWeightClass if 'OS/2' in font else None,
            'units_per_em':font['head'].unitsPerEm if 'head' in font else None}
    font.close();return result


def to_alpha(path,size=256):
    p=Path(path)
    if p.suffix.lower()=='.svg':
        import cairosvg
        raw=cairosvg.svg2png(url=str(p),output_width=size,output_height=size)
        im=Image.open(io.BytesIO(raw)).convert('RGBA')
    else:im=Image.open(p).convert('RGBA').resize((size,size),Image.Resampling.LANCZOS)
    alpha=np.asarray(im.getchannel('A'))
    # If an opaque raster icon is given, use a luminance threshold instead.
    if (alpha>245).mean()>.98:
        gray=np.asarray(im.convert('L'))
        alpha=255-gray
    return alpha>127


def shape_metrics(reference,render):
    a=to_alpha(reference);b=to_alpha(render)
    if not a.any() or not b.any():raise ValueError('Icon render has no visible shape')
    inter=int((a&b).sum()); union=int((a|b).sum());iou=inter/union
    def bounds(mask):
        ys,xs=np.where(mask)
        return [int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1]
    ba=bounds(a);bb=bounds(b)
    # Mass and baseline differences matter even when overall silhouette is similar.
    baseline=abs(ba[3]-bb[3]);fill=abs(float(a.mean())-float(b.mean()))
    # Estimate interior thickness using erosion, independent of color. Optional SciPy.
    try:
        from scipy.ndimage import binary_erosion
        edge_a=a & ~binary_erosion(a);edge_b=b & ~binary_erosion(b)
        thickness_proxy_a=float(a.sum())/max(int(edge_a.sum()),1)
        thickness_proxy_b=float(b.sum())/max(int(edge_b.sum()),1)
        thick_delta=abs(thickness_proxy_a-thickness_proxy_b)
    except ImportError:thick_delta=None
    return {'silhouette_iou':round(iou,5),'reference_bounds':ba,'render_bounds':bb,
            'baseline_delta_px_256':baseline,'fill_delta':round(fill,5),
            'stroke_weight_proxy_delta':round(thick_delta,3) if thick_delta is not None else None,
            'style_mismatch_suspected':iou<.88 or baseline>6 or fill>.08}


def audit(config):
    data={'fonts':[],'icons':[],'notes':['Font files are read locally; no licensed font bytes leave the environment.','Missing original vectors must not be claimed recovered.']}
    for item in config.get('fonts',[]):
        if not Path(item['reference']).is_file() or not Path(item['render']).is_file():
            data['fonts'].append({'name':item['name'],'status':'missing source font','passed':None});continue
        a=font_metadata(item['reference']);b=font_metadata(item['render'])
        passed=bool(set(a['family'])&set(b['family'])) and a['weight_class']==b['weight_class']
        data['fonts'].append({'name':item['name'],'status':'compared','reference':a,'render':b,'passed':passed})
    for item in config.get('icons',[]):
        if not Path(item['reference']).is_file() or not Path(item['render']).is_file():
            data['icons'].append({'name':item['name'],'status':'missing source icon','passed':None});continue
        result=shape_metrics(item['reference'],item['render']);data['icons'].append({'name':item['name'],**result,'passed':not result['style_mismatch_suspected']})
    return data

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();r=audit(json.loads(args.config.read_text(encoding='utf-8')))
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(r,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(r,indent=2,ensure_ascii=False))