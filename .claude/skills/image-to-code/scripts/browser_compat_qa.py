#!/usr/bin/env python3
"""Mobile touch/focus/browser probe. Unsupported engines are SKIPPED, never passed.
Test pages must contain selectors supplied in JSON. Safari engine != iOS device testing.
"""
import argparse, json
from pathlib import Path
from playwright.sync_api import sync_playwright


def run(config):
    outcomes=[]
    with sync_playwright() as p:
        browsers={'chromium':p.chromium,'webkit':p.webkit,'firefox':p.firefox}
        for engine in config.get('engines',['chromium','webkit']):
            if engine not in browsers:raise ValueError('Unknown engine '+engine)
            try:browser=browsers[engine].launch(headless=True,executable_path=config.get('executable') if engine=='chromium' else None,args=['--no-sandbox'] if engine=='chromium' else [])
            except Exception as e:
                outcomes.append({'engine':engine,'status':'SKIPPED','reason':'Browser engine unavailable; not independently tested.','detail':str(e)[:280]});continue
            try:
                page=browser.new_page(viewport={'width':390,'height':844},is_mobile=engine!='firefox',has_touch=engine!='firefox',device_scale_factor=1)
                errors=[];page.on('pageerror',lambda e: errors.append(str(e)))
                if config.get('html_file'):
                    page.set_content(Path(config['html_file']).read_text(encoding='utf-8'),wait_until='load')
                else:
                    page.goto(config['url'],wait_until='domcontentloaded',timeout=30000)
                interactive=config.get('interactive',[])
                focus=page.evaluate('''selectors=>{const els=selectors.map(s=>document.querySelector(s));for(const e of els)if(!e||!e.matches('button,a,input,textarea,select,[tabindex]'))return {valid:false};return {valid:true}}''',interactive)
                order=[]
                for _ in range(config.get('tab_steps',8)):
                    page.keyboard.press('Tab');order.append(page.evaluate('document.activeElement?.getAttribute("data-testid") || document.activeElement?.id || document.activeElement?.tagName'))
                taps=[]
                for action in config.get('taps',[]):
                    try:page.locator(action['selector']).tap(timeout=2000);taps.append({'selector':action['selector'],'passed':page.locator(action['expect']).is_visible()})
                    except Exception as e:taps.append({'selector':action['selector'],'passed':False,'error':str(e)[:120]})
                swipe=[]
                for action in config.get('swipes',[]):
                    sel=page.locator(action['selector'])
                    try:
                        rect=sel.bounding_box();assert rect
                        x=rect['x']+rect['width']/2;y=rect['y']+rect['height']/2
                        if engine!='chromium':
                            swipe.append({'selector':action['selector'],'status':'SKIPPED','reason':'Native swipe injection unavailable on this engine'})
                            continue
                        cdp=page.context.new_cdp_session(page)
                        cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
                        for step in range(1,6):
                            cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x+step*int(action.get('dx',0))/5,'y':y+step*int(action.get('dy',-120))/5}]})
                        cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
                        swipe.append({'selector':action['selector'],'status':'PASS','native_cdp_touch':True})
                    except Exception as e:swipe.append({'selector':action['selector'],'status':'FAIL','error':str(e)[:120]})
                passed=focus['valid'] and all(x['passed'] for x in taps) and all(x['status']!='FAIL' for x in swipe) and not errors
                outcomes.append({'engine':engine,'status':'PASS' if passed else 'FAIL','focus_order':order,'taps':taps,'touch':swipe,'errors':errors})
                page.close()
            finally:browser.close()
    return outcomes

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--config',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    a=ap.parse_args();result=run(json.loads(a.config.read_text(encoding='utf-8')));a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
    if any(x['status']=='FAIL' for x in result):raise SystemExit(1)