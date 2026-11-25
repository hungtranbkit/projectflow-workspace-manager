const {test}=require("node:test");
const assert=require("node:assert/strict");
const {index,view,key}=require("../app/static/cc95-map.js");
const item=(project,source,name,node="n1",fresh=true)=>({project,source,name,node,fresh});
test("hierarchy uses canonical project ids; groups are observed sources, not modules",()=>{
const rows=[item("a/repo","git","same"),item("b/repo","git","same"),item("a/repo","docker","c","n2",false),item("a/repo","git","same","n2"),item("a/repo","git","same")];
const p=index({projects:["a/repo","b/repo","empty/repo"],items:rows});
assert.equal(p.length,3);
assert.equal(view(p,[]).level,0);
assert.equal(view(p,["a/repo"]).p.count,3);
assert.equal(view(p,["a/repo"]).p.stale,1);
assert.equal(view(p,["a/repo","git"]).children.length,2);
const k=key(rows[3]);assert.equal(view(p,["a/repo","git",k]).item.row.node,"n2");
assert.equal(view(p,["missing"]).level,0);
assert.equal(view(p,["a/repo","missing"]).level,1);
assert.equal(view(p,["empty/repo"]).children.length,0);
});
test("unmapped inventory stays separate; invalid sources ignored and unknown freshness stale",()=>{
const p=index({projects:[],items:[item("","android","x"),{project:"a",source:"unknown",name:"x",node:"n"},{project:"a",source:"git",name:"x",node:"n"}]});
assert.equal(p.length,2);
assert.equal(view(p,[""]).p.label,"Chưa gắn dự án");
assert.equal(view(p,["a"]).p.stale,1);
});
test("distinct names cannot collide through punctuation in composite id",()=>{
assert.notEqual(key({node:"a:b",source:"git",name:"x"}),key({node:"a",source:"git",name:"b:x"}));
});

for(const node of ["","m910"])test("initial project and node scope survive first render: "+(node||"all"),async()=>{
 const fs=require("node:fs"),vm=require("node:vm");
 class Element {
   constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.textContent="";this.value="";this.listeners={};}
   append(...nodes){this.children.push(...nodes);}
   replaceChildren(...nodes){this.children=nodes;}
   addEventListener(k,f){this.listeners[k]=f;}
   setAttribute(){}
   focus(){}
 }
 const host=new Element("section");host.dataset.project="example/repo";host.dataset.node=node;
 const selectors=new Map();host.querySelector=s=>{if(!selectors.has(s))selectors.set(s,new Element(s));return selectors.get(s);};
 let resolve;
 const response=new Promise(r=>{resolve=r;});
 let requestPath;let nextResponse=response;
 const sandbox={
   document:{getElementById:id=>id==="cc95-map"?host:null,createElement:tag=>new Element(tag)},
   fetch:async(path)=>{requestPath=path;return nextResponse;},URLSearchParams,Date
 };
 vm.runInNewContext(fs.readFileSync(require.resolve("../app/static/cc95-map.js"),"utf8"),sandbox);
 const root=node?"Dự án trên "+node:"Toàn bộ hệ thống";
 assert.equal(requestPath,"/api/steward?intent=now"+(node?"&node="+node:""));
 assert.equal(host.querySelector(".cc-center").children[1].textContent,root);
 resolve({ok:true,json:async()=>({projects:["example/repo"],items:[item("example/repo","git","checkout")],at:1})});
 await new Promise(r=>setImmediate(r));
 assert.equal(host.querySelector(".cc-center").children[1].textContent,"example/repo");
 assert.equal(host.querySelector(".cc-branches").children.length,1);
 assert.equal(host.querySelector(".cc-branches").children[0].children[0].textContent,"Mã nguồn");
 nextResponse=Promise.resolve({ok:false,status:401});
 await host.querySelector(".cc-reload").listeners.click();
 assert.equal(host.querySelector(".cc-center").children[1].textContent,root);
 assert.equal(host.querySelector(".cc-tree").children.length,1);
 assert.match(host.querySelector(".cc-error").textContent,/401/);
 nextResponse=Promise.resolve({ok:true,json:async()=>({projects:["example/repo"],items:[item("example/repo","git","checkout",node||"n1")],at:2})});
 await host.querySelector(".cc-reload").listeners.click();
 assert.equal(host.querySelector(".cc-center").children[1].textContent,"example/repo");
 assert.equal(host.querySelector(".cc-error").textContent,"");
 host.querySelector(".cc-up").listeners.click();
 assert.equal(host.querySelector(".cc-center").children[1].textContent,root);
});
