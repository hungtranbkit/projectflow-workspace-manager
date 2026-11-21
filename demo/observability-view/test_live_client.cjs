"use strict";
/* No browser dependency or network: check live client against a fake DOM. */
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const path=process.argv[2] || "demo/observability-view/live.js";
const ids=["live-health","live-time","live-nodes","live-open","live-signal","live-acceptance","live-ttl","live-message","live-panel","live-node-list","live-refresh"];
class Element {
  constructor(id){this.id=id;this.textContent="";this.dataset={};this.children=[];this.listeners={};this.className=""}
  addEventListener(k,f){this.listeners[k]=f}
  replaceChildren(){this.children=[]}
  append(...items){this.children.push(...items)}
}
const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
const doc={baseURI:"https://hungtranbkit.github.io/projectflow-workspace-manager/",hidden:false,
  listeners:{},getElementById(id){if(!elements[id])throw Error("missing "+id);return elements[id]},
  createElement(k){return new Element(k)},addEventListener(k,fn){this.listeners[k]=fn}};
const instant=new Date().toISOString();
const snapshot={schema_version:1,kind:"terminal-mcp-public-aggregate",observed_at:instant,
  nodes:{total:2,online:1,offline:1,unknown:0,
    items:[{label:"Node 01",status:"online",open_sessions:2,recent_activity_signals:1},
           {label:"Node 02",status:"offline",open_sessions:0,recent_activity_signals:0}]},
  sessions:{open:2,recent_activity_signals:1,running_verified:null},
  acceptance:{percent:null,ttl_percent:null}};
let fetched=0;
const context={document:doc,Date,Intl,URL,Number,Object,Array,Error,String,
 setInterval(){return 1},
 fetch:async function(url,args){
  assert.ok(String(url).endsWith("/projectflow-workspace-manager/live-status.json"));
  assert.equal(args.credentials,"omit");fetched++;
  return {ok:true,json:async()=>snapshot};
 }};
const source=fs.readFileSync(path,"utf8");
vm.runInNewContext(source,context);
(async()=>{await new Promise(setImmediate);
 await Promise.resolve();await Promise.resolve();await Promise.resolve();
 assert.equal(elements["live-nodes"].textContent,"1 / 2");
 assert.equal(elements["live-open"].textContent,"2");
 assert.equal(elements["live-signal"].textContent,"1");
 assert.equal(elements["live-acceptance"].textContent,"UNKNOWN");
 assert.equal(elements["live-ttl"].textContent,"UNKNOWN");
 assert.equal(elements["live-panel"].dataset.health,"fresh");
 assert.equal(elements["live-node-list"].children.length,2);
 assert.ok(fetched>=1);
 assert.ok(source.includes("connect")===false || source.includes("No private API"));
 console.log("LIVE_CLIENT_PASS valid snapshot, no private data, evidence UNKNOWN");
})().catch(e=>{console.error(e);process.exitCode=1});
