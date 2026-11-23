#!/usr/bin/env python3
"""Compare browser typography including wrapping, effective fallbacks and supplied files.
Cannot conclusively detect fonts sharing identical advances; marks those results uncertain.
"""
import argparse,json
from pathlib import Path
from playwright.sync_api import sync_playwright


def audit(cfg):
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=cfg.get('chromium_executable'),args=['--no-sandbox'] if cfg.get('chromium_executable') else [])
  page=browser.new_page(viewport=cfg.get('viewport',{'width':390,'height':844}))
  if cfg.get('html_file'):page.set_content(Path(cfg['html_file']).read_text(encoding='utf8'),wait_until='load')
  else:page.goto(cfg['url'],wait_until='load')
  page.evaluate('document.fonts.ready')
  checks=[]
  for item in cfg.get('selectors',[]):
   result=page.evaluate('''arg=>{
    const el=document.querySelector(arg.selector);if(!el)return {status:'NOT_FOUND'};
    const cs=getComputedStyle(el),node=document.createRange();node.selectNodeContents(el);
    const lines=[...node.getClientRects()].map(r=>Math.round(r.top));
    const c=document.createElement('canvas').getContext('2d');
    const text=el.textContent.trim();c.font=cs.font;
    const w=c.measureText(text).width;
    const generic={};for(const face of ['serif','sans-serif','monospace']){c.font=`${cs.fontWeight} ${cs.fontSize} ${face}`;generic[face]=c.measureText(text).width}
    const family=cs.fontFamily.split(',')[0].replaceAll('"','').replaceAll("'",'').trim();
    const loaded=document.fonts.check(`${cs.fontWeight} ${cs.fontSize} "${family}"`);
    return {status:'OBSERVED',computed_family:cs.fontFamily,font_size:cs.fontSize,font_weight:cs.fontWeight,line_height:cs.lineHeight,letter_spacing:cs.letterSpacing,line_count:new Set(lines).size,measured_text_width_px:Math.round(w*100)/100,document_fonts_check:loaded,generic_widths:generic};
   }''',item)
   if result['status']=='OBSERVED':
    warnings=[]
    if item.get('expected_family') and item['expected_family'].lower() not in result['computed_family'].lower():warnings.append('Computed family differs')
    if item.get('expected_lines') and item['expected_lines']!=result['line_count']:warnings.append('Text wrapping differs')
    if item.get('expected_weight') and str(item['expected_weight'])!=result['font_weight']:warnings.append('Weight differs')
    if item.get('expected_size_px') and abs(float(result['font_size'].replace('px',''))-float(item['expected_size_px']))>.6:warnings.append('Size differs')
    if not result['document_fonts_check']:warnings.append('Requested face unavailable')
    result['warnings']=warnings
    result['verified_metadata_only']=not warnings
    result['note']='Computed family and FontFaceSet checks cannot definitively exclude a same-metric fallback. Compare licensed local font metadata if present.'
   checks.append({'selector':item['selector'],**result})
  browser.close()
  return {'checks':checks,'verified':all(x['status']=='OBSERVED' and not x['warnings'] for x in checks),
          'note':'Wrapping and computed font audit, not a proof of exact original font bytes.'}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
 a=p.parse_args();r=audit(json.loads(a.config.read_text(encoding='utf8')));a.out.parent.mkdir(parents=True,exist_ok=True)
 a.out.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r,indent=2))
 if not r['verified']:raise SystemExit(1)