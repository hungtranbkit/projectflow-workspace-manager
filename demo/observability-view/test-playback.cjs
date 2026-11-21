"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
class Element {
  constructor(id=""){this.id=id;this.textContent="";this.dataset={};this.style={};this.children=[];this.events={};this.attributes={};
    this.classList={active:new Set(),toggle:(k,flag)=>{if(flag)this.classList.active.add(k);else this.classList.active.delete(k)}};}
  addEventListener(t,f){this.events[t]=f}
  setAttribute(k,v){this.attributes[k]=v}
  replaceChildren(...items){this.children=items}
  append(...items){this.children.push(...items)}
  appendChild(item){this.children.push(item);return item}
  click(){assert.equal(typeof this.events.click,"function","missing click listener for "+this.id);this.events.click()}
}
const ids=["workflow","task-name","step-counter","status-code","status-title","status-detail","gate-value","gate-detail","ttl-value","ttl-detail","claude","codex","harness","evidence","flow-bar","gate-bar","ttl-bar","steps","events","observed","next","play","reset","speed"];
const map=Object.fromEntries(ids.map(x=>[x,new Element(x)]));
const names=["normal","stuck","offline"];
const buttons=names.map(n=>{let e=new Element("scenario-"+n);e.dataset.scenario=n;return e});
const document={
 getElementById:id=>{assert.ok(map[id],"element "+id+" missing");return map[id]},
 querySelectorAll:query=>{assert.equal(query,"[data-scenario]");return buttons},
 createElement:tag=>new Element(tag),
 addEventListener:(type,fn)=>{document.events[type]=fn},
 events:{},hidden:false
};
let ticks=[],nextId=0;
const context={document,setTimeout:(fn,ms)=>{ticks.push({fn,ms,id:++nextId});return nextId},clearTimeout:()=>{ticks=[]}};
const src=fs.readFileSync(process.argv[2]||"app.js","utf8");
vm.runInNewContext(src,context);
const text=id=>map[id].textContent;
assert.equal(text("status-code"),"QUEUED");assert.equal(text("gate-value"),"0%");
assert.equal(map.play.attributes["aria-pressed"],"true");
assert.equal(ticks.length,1);assert.equal(ticks[0].ms,4000);
map.speed.click();assert.equal(text("speed"),"2× tốc độ");
assert.equal(ticks.length,1);assert.equal(ticks[0].ms,2000);
map.next.click();assert.equal(text("status-code"),"DISPATCHED");
assert.equal(map.play.attributes["aria-pressed"],"false");
assert.equal(text("gate-value"),"0%");
buttons[1].click();assert.equal(text("status-code"),"QUEUED");
for(let i=0;i<5;i++)map.next.click();
assert.equal(text("status-code"),"BLOCKED");
assert.equal(text("gate-value"),"0%");assert.equal(text("ttl-value"),"100%");
assert.equal(text("observed"),"Chưa có bằng chứng mới");
assert.equal(map.events.children.length,6);
assert.equal(map.next.disabled,true);
buttons[2].click();for(let i=0;i<5;i++)map.next.click();
assert.equal(text("status-code"),"UNKNOWN");assert.equal(text("gate-value"),"0%");
buttons[0].click();for(let i=0;i<5;i++)map.next.click();
assert.equal(text("status-code"),"ACCEPTED");assert.equal(text("gate-value"),"100%");
assert.equal(text("ttl-value"),"88%");
assert.ok(text("evidence").includes("dữ liệu mẫu"));
map.reset.click();assert.equal(text("status-code"),"QUEUED");
buttons[1].click();document.hidden=true;document.events.visibilitychange();
assert.equal(map.play.attributes["aria-pressed"],"false");
assert.equal(ticks.length,0);
assert.ok(src.indexOf("fetch(")===-1 && src.indexOf("XMLHttpRequest")===-1 && src.indexOf("WebSocket")===-1);
console.log("DEMO_PLAYBACK_TEST_PASS: normal/stuck/offline, 6 stages, pause/next/reset, speed, hidden tab, no network API");