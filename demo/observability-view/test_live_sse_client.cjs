"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element{
 constructor(id){this.id=id;this.textContent="";this.dataset={};this.style={};this.children=[];this.events={};this.className="";}
 addEventListener(k,f){this.events[k]=f;}
 replaceChildren(...items){this.children=items;}
 append(...items){this.children.push(...items);}
 appendChild(item){this.children.push(item);return item;}
}
const ids=["live-panel","live-health","live-time","live-nodes","live-open","live-signal",
 "live-one-minute","live-acceptance","live-ttl","live-transport","live-message",
 "live-node-list","live-sessions","live-sessions-note","live-changes","live-refresh"];
const els=Object.fromEntries(ids.map(i=>[i,new Element(i)]));
const document={hidden:false,listeners:{},getElementById:i=>{assert.ok(els[i],"missing DOM #"+i);return els[i]},
 createElement:type=>new Element(type),addEventListener(k,f){this.listeners[k]=f;}};
let source;
class EventSource{
 constructor(url,options){assert.equal(url,"https://terminal-bootstrap.mesflow.net/projectflow-live/events");
   assert.equal(options.withCredentials,false);source=this;this.listeners={};}
 addEventListener(type,fn){this.listeners[type]=fn;}
 emit(type,data){const fn=this.listeners[type];assert.ok(fn,type);fn({data:JSON.stringify(data)});}
}
const values=()=>({
 schema_version:2,kind:"terminal-mcp-public-aggregate",observed_at:new Date().toISOString(),
 nodes:{total:2,online:1,degraded:1,offline:0,unknown:0,items:[
 {label:"Node 01",status:"online",capacity:"healthy",heartbeat_age_seconds:2,
  open_sessions:2,recent_activity_signals:1,signals_last_minute:1,
  agent_types:{claude:1,codex:1,shell:0,other:0}},
 {label:"Node 02",status:"degraded",capacity:"overloaded",heartbeat_age_seconds:5,
  open_sessions:1,recent_activity_signals:0,signals_last_minute:0,
  agent_types:{claude:0,codex:0,shell:1,other:0}}]},
 sessions:{open:3,recent_activity_signals:1,recent_last_minute:1,running_verified:null,
   agent_types:{claude:1,codex:1,shell:1,other:0},items:[
   {label:"Session 01",node:"Node 01",agent_type:"claude",activity_bucket:"under_1m",running_verified:null},
   {label:"Session 02",node:"Node 01",agent_type:"codex",activity_bucket:"over_5m",running_verified:null}
 ]},
 acceptance:{percent:null,ttl_percent:null}
});
const ctx={document,window:{EventSource},EventSource,Date,Intl,Object,Array,Number,String,URL,
 fetch:async()=>{throw new Error("Fallback must not be called while stream works");},
 setInterval:()=>1};
const js=fs.readFileSync(process.argv[2]||"live-sse.js","utf8");
vm.runInNewContext(js,ctx);
assert.ok(source,"EventSource connected");
source.emit("status",values());
assert.equal(els["live-nodes"].textContent,"1 / 2");
assert.equal(els["live-open"].textContent,"3");
assert.equal(els["live-signal"].textContent,"1");
assert.equal(els["live-one-minute"].textContent,"1");
assert.equal(els["live-acceptance"].textContent,"UNKNOWN");
assert.equal(els["live-ttl"].textContent,"UNKNOWN");
assert.equal(els["live-panel"].dataset.health,"fresh");
assert.equal(els["live-node-list"].children.length,2);
assert.equal(els["live-sessions"].children.length,2);
assert.ok(els["live-transport"].textContent.includes("SSE"));
const named=values();
named.nodes.items[0].label="hp-linux";
named.nodes.items[1].label="m910";
named.sessions.items[0].node="hp-linux";
named.sessions.items[1].node="m910";
source.emit("status",named);
assert.equal(els["live-node-list"].children[0].children[0].textContent,"hp-linux");
assert.equal(els["live-node-list"].children[1].children[0].textContent,"m910");
assert.equal(els["live-sessions"].children[0].children[1].textContent.includes("hp-linux"),true);
const changed=values();changed.sessions.open=4;changed.nodes.items[0].open_sessions=3;
source.emit("status",changed);
assert.ok(els["live-changes"].children.length>=1);
const bad=values();bad.acceptance.percent=100;
source.emit("status",bad);
assert.equal(els["live-panel"].dataset.health,"unknown");
assert.equal(els["live-acceptance"].textContent,"UNKNOWN");
assert.equal(els["live-open"].textContent,"—");
assert.ok(!js.includes("fetch('/dashboard") && !js.includes("session_name"));
console.log("LIVE_SSE_CLIENT_PASS stream detail, degraded, UNKNOWN, change history, secret-safe DOM");
