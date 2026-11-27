#!/usr/bin/env node
/** Browser geometry contract for visual states. A state only passes if critical regions
 * and their interactive descendants fit the screenshot viewport and are unobstructed.
 * No website-specific selectors are hardcoded.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export function parseGeometryConfig(config) {
  if (!config || typeof config !== 'object' || !Array.isArray(config.states) || !config.states.length) throw Error('states[] required');
  if (!Array.isArray(config.viewports) || !config.viewports.length) throw Error('viewports[] required');
  for (const viewport of config.viewports) {
    if (!Number.isInteger(viewport.width) || viewport.width < 240 || !Number.isInteger(viewport.height) || viewport.height < 240) throw Error('Invalid viewport');
  }
  for (const state of config.states) {
    if (!state.name || !Array.isArray(state.mustFit) || !state.mustFit.length) throw Error('Each state needs name and nonempty mustFit[]');
    if ((state.clicks ?? []).some(x=>typeof x!=='string' || !x.trim())) throw Error('Invalid clicks[]');
  }
  return config;
}

export function inspectBox(box, windowSize, tolerance=1) {
  if (!box || box.width<=0 || box.height<=0) return { passed:false, reason:'missing or zero-size' };
  const passed=box.left>=-tolerance && box.top>=-tolerance && box.right<=windowSize.width+tolerance && box.bottom<=windowSize.height+tolerance;
  return {passed,reason:passed?'within viewport':`clipped: ${JSON.stringify(box)}`};
}

export async function runVisualStates(config, chromium) {
  parseGeometryConfig(config);
  const out=path.resolve(config.out??'artifacts/visual-states');
  await fs.mkdir(out,{recursive:true});
  const browser=await chromium.launch({headless:true});
  const checks=[];
  try {
    for(const size of config.viewports) {
      const context=await browser.newContext({viewport:size,deviceScaleFactor:1,reducedMotion:'reduce'});
      try {
        for(const state of config.states) {
          const page=await context.newPage();
          const errors=[];
          page.on('pageerror',e=>errors.push(e.message));
          try {
            await page.goto(config.url,{waitUntil:'domcontentloaded',timeout:30000});
            await page.evaluate(()=>document.fonts.ready);
            for(const selector of state.clicks??[]) await page.locator(selector).click();
            await page.waitForTimeout(100);
            const result=await page.evaluate((selectors)=>{
              const entries=[];
              for(const selector of selectors) {
                const node=document.querySelector(selector);
                const rect=node?.getBoundingClientRect();
                const style=node&&getComputedStyle(node);
                const visible=!!node && style.display!=='none' && style.visibility!=='hidden' && Number(style.opacity)!==0 && rect.width>0 && rect.height>0;
                const box=rect?{left:rect.left,top:rect.top,right:rect.right,bottom:rect.bottom,width:rect.width,height:rect.height}:null;
                // Detect clipping ancestors that hide the element even when its own box fits.
                let clipped=false;
                if (node && box) for(let p=node.parentElement;p;p=p.parentElement) {
                  const s=getComputedStyle(p);
                  if(['hidden','clip','scroll','auto'].includes(s.overflowX)||['hidden','clip','scroll','auto'].includes(s.overflowY)) {
                    const r=p.getBoundingClientRect();
                    if(box.top<r.top-1||box.bottom>r.bottom+1||box.left<r.left-1||box.right>r.right+1){clipped=true;break;}
                  }
                }
                entries.push({selector,visible,box,clipped});
              }
              return {entries,brokenImages:[...document.images].filter(im=>im.getClientRects().length&&(!im.complete||!im.naturalWidth)).map(im=>im.src),horizontalOverflow:document.documentElement.scrollWidth>innerWidth+1};
            },state.mustFit);
            const conditions=result.entries.map(x=>({selector:x.selector,...inspectBox(x.box,size),visible:x.visible,clipped:x.clipped}));
            const passed=conditions.every(c=>c.passed&&c.visible&&!c.clipped)&&!errors.length&&!result.brokenImages.length&&!result.horizontalOverflow;
            const filename=`${state.name.replace(/[^\w-]/g,'_')}-${size.width}x${size.height}.png`;
            await page.screenshot({path:path.join(out,filename),animations:'disabled'});
            checks.push({state:state.name,viewport:size,passed,conditions,brokenImages:result.brokenImages,errors,horizontalOverflow:result.horizontalOverflow,screenshot:filename});
          }catch(e){checks.push({state:state.name,viewport:size,passed:false,error:String(e)});}
          finally{await page.close();}
        }
      } finally {await context.close();}
    }
  }finally{await browser.close();}
  const report={passed:checks.every(x=>x.passed),checks,note:'Geometry gate, not visual fidelity score. Check pixel comparison separately.'};
  await fs.writeFile(path.join(out,'visual-states-report.json'),JSON.stringify(report,null,2)+'\n');
  return report;
}

if(process.argv[1]&&fileURLToPath(import.meta.url)===path.resolve(process.argv[1])){
  try{
    const args=process.argv.slice(2);if(args.length!==1)throw Error('Usage: node scripts/validate_visual_states.mjs <config.json>');
    const cfg=parseGeometryConfig(JSON.parse(await fs.readFile(args[0],'utf-8')));
    const {chromium}=await import('playwright');
    const report=await runVisualStates(cfg,chromium);
    console.log(JSON.stringify({passed:report.passed,checks:report.checks.length}));
    if(!report.passed)process.exitCode=1;
  }catch(e){console.error(String(e));process.exitCode=1;}
}
