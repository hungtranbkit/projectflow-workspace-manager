"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element{
 constructor(id){this.id=id;this.textContent="";this.dataset={};this.style={};this.children=[];this.events={};this.className="";}
 addEventListener(k,f){this.events[k]=f;}
 replaceChildren(...items){this.children=items;}
 append(...items){this.children.push(...items);}
 appendChild(item){this.children.push(item);return item;}
 setAttribute(k,v){this.attributes??={};this.attributes[k]=v;}
}
const ids=["live-panel","live-health","live-time","live-nodes","live-open","live-signal",
 "live-one-minute","live-acceptance","live-ttl","live-transport","live-message",
 "live-node-list","live-sessions","live-sessions-note","live-projects","live-changes","live-refresh",
 "live-running-count","live-running-note","live-task-coverage","live-state-filters","live-show-more"];
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
function enrich(d){
  d.sessions.projects=[{name:"Zalo Group Hub",open_sessions:1},{name:"ProjectFlow",open_sessions:1}];
  d.sessions.proven_running=0;
  d.sessions.running_verification="checkpoint_only_when_recent";
  d.sessions.items.forEach((s,i)=>Object.assign(s,{
    session:i===0?"zgh-photo-capacity-fix-20261121":"projectflow-observability-20261120",
    project:i===0?"Zalo Group Hub":"ProjectFlow",
    task_hint:i===0?"Sửa lỗi giới hạn và chuyển tiếp ảnh":"Xây dựng/kiểm tra theo dõi agent",
    task_source:"session_name_hint",execution_state:"OPEN_UNVERIFIED",
    evidence:"registry_only",commit_ref:i===0?"abcdef123456":null,
    branch_ref:i===0?"feat/photo-fix":null,
    last_activity_at:null,last_checkpoint_at:null
  }));
  return d;
}
const ctx={document,window:{EventSource},EventSource,Date,Intl,Object,Array,Number,String,URL,
 fetch:async()=>{throw new Error("Fallback must not be called while stream works");},
 setInterval:()=>1};
const js=fs.readFileSync(process.argv[2]||"live-sse.js","utf8");
vm.runInNewContext(js,ctx);
assert.ok(source,"EventSource connected");
source.emit("status",enrich(values()));
assert.equal(els["live-nodes"].textContent,"1 / 2");
assert.equal(els["live-open"].textContent,"3");
assert.equal(els["live-signal"].textContent,"1");
assert.equal(els["live-one-minute"].textContent,"1");
assert.equal(els["live-acceptance"].textContent,"UNKNOWN");
assert.equal(els["live-ttl"].textContent,"UNKNOWN");
assert.equal(els["live-panel"].dataset.health,"fresh");
assert.equal(els["live-node-list"].children.length,2);
assert.equal(els["live-sessions"].children.length,2);
assert.ok(els["live-sessions"].children[0].children.some(x=>x.textContent.includes("Sửa lỗi giới hạn và chuyển tiếp ảnh")));
assert.ok(els["live-projects"].children.length>=3);
assert.ok(els["live-sessions"].children[0].children.some(x=>x.textContent.includes("chưa xác minh RUNNING")));
assert.ok(els["live-transport"].textContent.includes("SSE"));
const named=enrich(values());
named.nodes.items[0].label="hp-linux";
named.nodes.items[1].label="m910";
named.sessions.items[0].node="hp-linux";
named.sessions.items[1].node="m910";
named.sessions.task_label_verified=1;
named.sessions.agent_identified=2;
source.emit("status",named);
assert.ok(els["live-task-coverage"].textContent.includes("1/3"));
assert.ok(els["live-task-coverage"].textContent.includes("2/3"));
assert.equal(els["live-node-list"].children[0].children[0].textContent,"hp-linux");
assert.equal(els["live-node-list"].children[1].children[0].textContent,"m910");
assert.equal(els["live-sessions"].children[0].children[1].textContent.includes("hp-linux"),true);
// Node cards are ranked by current connection status, never registry order.
const ordered=enrich(values());
const makeNode=(label,status)=>({...ordered.nodes.items[0],label,status});
ordered.nodes.items=[
  makeNode("local","offline"), makeNode("m910","online"),
  makeNode("win","unknown"), makeNode("nec1","degraded"),
  makeNode("hp-linux","online"), makeNode("dell-linux","online"),
  makeNode("macbook","offline")
];
ordered.nodes.total=7;ordered.nodes.online=3;ordered.nodes.degraded=1;
ordered.nodes.unknown=1;ordered.nodes.offline=2;
const sourceOrder=ordered.nodes.items.map(n=>n.label);
source.emit("status",ordered);
assert.deepEqual(els["live-node-list"].children.map(n=>n.children[0].textContent),
  ["dell-linux","hp-linux","m910","nec1","win","local","macbook"]);
assert.deepEqual(ordered.nodes.items.map(n=>n.label),sourceOrder,
  "render must not modify registry/SSE input");
const verified=enrich(values());
verified.sessions.proven_running=1;
verified.sessions.items[1].execution_state="RUNNING_VERIFIED";
verified.sessions.items[1].running_verified=true;
verified.sessions.items[1].evidence="verified_recent_checkpoint";
verified.sessions.items[1].last_checkpoint_at=new Date().toISOString();
source.emit("status",verified);
assert.equal(els["live-running-count"].textContent,"1");
assert.equal(els["live-sessions"].children[0].children[0].children[0].textContent,
  "projectflow-observability-20261120","verified RUNNING must sort first");
const runningButton=els["live-state-filters"].children.find(b=>b.textContent.startsWith("RUNNING"));
assert.ok(runningButton);
runningButton.events.click();
assert.equal(els["live-sessions"].children.length,1,"RUNNING filter must hide unverified cards");
const allButton=els["live-state-filters"].children.find(b=>b.textContent.startsWith("Tất cả"));
allButton.events.click();
assert.equal(els["live-sessions"].children.length,2);
const observed=enrich(values());
observed.sessions.items[0].execution_state="IDLE";
observed.sessions.items[0].evidence="node_observed";
observed.sessions.items[0].task_source="trusted_task_label";
observed.sessions.items[0].task_hint="Hoàn thiện giám sát hoạt động agent";
observed.sessions.items[1].execution_state="RUNNING_VERIFIED";
observed.sessions.items[1].running_verified=true;
observed.sessions.items[1].evidence="terminal_output_delta";
observed.sessions.items[1].last_checkpoint_at=new Date().toISOString();
observed.sessions.proven_running=1;
source.emit("status",observed);
assert.equal(els["live-running-count"].textContent,"1");
assert.ok(els["live-sessions"].children[0].children.some(x=>
  x.textContent.includes("Đã quan sát nội dung terminal thay đổi")));
assert.ok(els["live-sessions"].children[1].children.some(x=>
  x.textContent.includes("Task được gắn gần nhất (chưa xác nhận công việc hiện tại): Hoàn thiện giám sát hoạt động agent")));
const uiProof=enrich(values());
uiProof.sessions.items[1].execution_state="RUNNING_VERIFIED";
uiProof.sessions.items[1].running_verified=true;
uiProof.sessions.items[1].evidence="agent_ui_busy";
uiProof.sessions.items[1].last_checkpoint_at=new Date().toISOString();
uiProof.sessions.proven_running=1;
source.emit("status",uiProof);
assert.equal(els["live-running-count"].textContent,"1");
assert.ok(els["live-sessions"].children[0].children.some(x=>
  x.textContent.includes("Claude/Codex hiển thị đang xử lý")));
const weakProof=els["live-sessions"].children[0];
assert.ok(weakProof.children[0].children[1].textContent.includes("Terminal/CLI hoạt động"));
assert.ok(weakProof.children[0].children[1].className.includes("unverified"));
const fullScreen=enrich(values());
fullScreen.sessions.items[1].execution_state="RUNNING_VERIFIED";
fullScreen.sessions.items[1].running_verified=true;
fullScreen.sessions.items[1].evidence="terminal_screen_delta";
fullScreen.sessions.items[1].last_checkpoint_at=new Date().toISOString();
fullScreen.sessions.proven_running=1;
source.emit("status",fullScreen);
assert.ok(els["live-sessions"].children[0].children.some(x=>
  x.textContent.includes("Toàn màn hình terminal thay đổi")));
const safeWork=enrich(values());
safeWork.sessions.items[1].execution_state="RUNNING_VERIFIED";
safeWork.sessions.items[1].running_verified=true;
safeWork.sessions.items[1].evidence="agent_ui_busy";
safeWork.sessions.items[1].last_checkpoint_at=new Date().toISOString();
safeWork.sessions.items[1].recent_actions=[
  "Có dòng báo đang chạy kiểm thử", "Có dòng báo build hoặc biên dịch"
];
safeWork.sessions.proven_running=1;
source.emit("status",safeWork);
const safeCard=els["live-sessions"].children[0];
const safeRows=safeCard.children.find(x=>x.className==="session-activity-hints");
assert.ok(safeRows,"Running card must show filtered activity hints");
assert.equal(safeRows.children.length,3,"Heading + two safe action labels");
assert.ok(safeRows.children[0].textContent.includes("8 dòng cuối"));
assert.ok(safeRows.children[1].textContent.includes("đang chạy kiểm thử"));
const unsafeWork=enrich(values());
unsafeWork.sessions.items[1].execution_state="RUNNING_VERIFIED";
unsafeWork.sessions.items[1].running_verified=true;
unsafeWork.sessions.items[1].evidence="agent_ui_busy";
unsafeWork.sessions.items[1].recent_actions=["api_key=private-token"];
unsafeWork.sessions.proven_running=1;
source.emit("status",unsafeWork);
assert.equal(els["live-panel"].dataset.health,"unknown",
  "Client must reject arbitrary terminal text in a public hint");
const idleWork=enrich(values());
idleWork.sessions.items[0].recent_actions=["Có dòng báo build hoặc biên dịch"];
source.emit("status",idleWork);
assert.equal(els["live-panel"].dataset.health,"unknown",
  "IDLE may not carry previous RUNNING action hints");
const many=enrich(values());
for(let i=3;i<=30;i++){
  const item={...many.sessions.items[0],label:"Session "+String(i).padStart(2,"0"),
    session:"zgh-photo-capacity-fix-"+i};
  many.sessions.items.push(item);
}
many.sessions.open=30;
many.sessions.displayed_count=30;
many.sessions.hidden_count=0;
many.sessions.proven_running=0;
many.sessions.nodes_covered=2;
many.sessions.coverage={covered_nodes:["hp-linux","m910"],unreachable_nodes:["nec1"]};
many.nodes.items[0].open_sessions=29;
many.nodes.items[1].open_sessions=1;
source.emit("status",many);
assert.equal(els["live-sessions"].children.length,24,"mobile starts with at most 24 cards");
assert.equal(els["live-show-more"].hidden,false);
assert.ok(els["live-show-more"].textContent.includes("6 phiên"));
assert.ok(els["live-message"].textContent.includes("2 node"));
els["live-show-more"].onclick();
assert.equal(els["live-sessions"].children.length,30);
assert.equal(els["live-show-more"].hidden,true);
const changed=enrich(values());changed.sessions.open=4;changed.nodes.items[0].open_sessions=3;
source.emit("status",changed);
assert.ok(els["live-changes"].children.length>=1);
const bad=enrich(values());bad.acceptance.percent=100;
source.emit("status",bad);
assert.equal(els["live-panel"].dataset.health,"unknown");
assert.equal(els["live-acceptance"].textContent,"UNKNOWN");
assert.equal(els["live-open"].textContent,"—");
assert.ok(!js.includes("fetch('/dashboard") && !js.includes("launch_command") && !js.includes("auth_token_ref"));
console.log("LIVE_SSE_CLIENT_PASS stream detail, degraded, UNKNOWN, change history, secret-safe DOM");
