(function () {
"use strict";
const labels={git:"Mã nguồn",docker:"Container","systemd-user":"Dịch vụ người dùng","systemd-system":"Dịch vụ hệ thống",android:"Ứng dụng Android","phone-storage":"Lưu trữ thiết bị","control-path":"Đường điều khiển"};
const key=x=>JSON.stringify([x.node,x.source,x.name]);
const compare=(a,b)=>a.localeCompare(b,"vi");
const clean=x=>typeof x==="string"?x:"";
function index(data){
  const projects=new Map();
  function get(id){if(!projects.has(id))projects.set(id,{id,label:id||"Chưa gắn dự án",groups:new Map(),count:0,stale:0});return projects.get(id);}
  for(const p of Array.isArray(data.projects)?data.projects:[])if(typeof p==="string"&&p)get(p);
  const seen=new Set();
  for(const r of Array.isArray(data.items)?data.items:[]){
    if(!r||typeof r.name!=="string"||typeof r.node!=="string"||typeof r.source!=="string"||!Object.prototype.hasOwnProperty.call(labels,r.source))continue;
    const p=get(clean(r.project)), id=key(r), composite=JSON.stringify([p.id,id]);
    if(seen.has(composite))continue;seen.add(composite);
    if(!p.groups.has(r.source))p.groups.set(r.source,{id:r.source,label:labels[r.source],items:[],stale:0});
    const g=p.groups.get(r.source);g.items.push({id,label:r.name,row:r});
    p.count++;if(r.fresh!==true){p.stale++;g.stale++;}
  }
  for(const p of projects.values())for(const g of p.groups.values())g.items.sort((a,b)=>compare(a.label,b.label)||compare(a.id,b.id));
  return [...projects.values()].sort((a,b)=>compare(a.label,b.label));
}
function view(projects,path){
 const p=projects.find(x=>x.id===path[0]);
 const g=p&&p.groups.get(path[1]);
 const item=g&&g.items.find(x=>x.id===path[2]);
 if(item)return {level:3,title:item.label,children:[],item,p,g};
 if(g)return {level:2,title:g.label,children:g.items.map(x=>({id:x.id,label:x.label,count:1,stale:x.row.fresh===true?0:1,sub:x.row.node})),p,g};
 if(p)return {level:1,title:p.label,children:[...p.groups.values()].sort((a,b)=>compare(a.label,b.label)).map(x=>({id:x.id,label:x.label,count:x.items.length,stale:x.stale})),p};
 return {level:0,title:"Toàn bộ hệ thống",children:projects.map(x=>({id:x.id,label:x.label,count:x.count,stale:x.stale}))};
}
if(typeof module==="object"&&module.exports)module.exports={index,view,key};
if(typeof document==="undefined")return;
const host=document.getElementById("cc95-map");if(!host)return;
const find=sel=>host.querySelector(sel);
const el=(tag,t,cl)=>{const e=document.createElement(tag);if(t!==undefined)e.textContent=t;if(cl)e.className=cl;return e;};
const button=(t,fn,cl)=>{const b=el("button",t,cl);b.type="button";b.addEventListener("click",fn);return b;};
let projects=[],path=host.dataset.project?[host.dataset.project]:[],query="",page=0,asof=null,available=false,loading=false;
const setPath=p=>{path=p;query="";find(".cc-search").value="";page=0;render();find(".cc-center").focus();};
const fmt=t=>Number.isFinite(t)&&t>0?new Date(t*1000).toLocaleString("vi-VN",{timeZone:"Asia/Ho_Chi_Minh"}):"Chưa đo";
function render(){
 const v=view(projects,path);if(available)path=path.slice(0,v.level);
 const crumbs=find(".cc-crumbs");crumbs.replaceChildren();
 crumbs.append(button("Toàn hệ thống",()=>setPath([])));
 if(v.p)crumbs.append(button(v.p.label,()=>setPath([v.p.id])));
 if(v.g)crumbs.append(button(v.g.label,()=>setPath([v.p.id,v.g.id])));
 if(v.item)crumbs.append(el("span",v.item.label));
 const center=find(".cc-center");center.replaceChildren(el("small",["L0 · Toàn cảnh","L1 · Dự án","L2 · Nhóm quan sát","L3 · Bằng chứng"][v.level]),el("strong",v.title),el("span",v.level===3?"Bản ghi nguồn":v.children.length+" nhánh"));
 const list=find(".cc-branches");list.replaceChildren();
 const children=v.children.filter(x=>(x.label+" "+(x.sub||"")).toLowerCase().includes(query.toLowerCase()));
 const size=24;page=Math.min(page,Math.max(0,Math.ceil(children.length/size)-1));
 for(const x of children.slice(page*size,(page+1)*size)){
   const b=button("",()=>setPath(path.concat(x.id)),"cc-node");
   b.append(el("strong",x.label),el("small",(x.sub?x.sub+" · ":"")+x.count+" quan sát"+(x.stale?" · "+x.stale+" cũ":"")));
   b.dataset.freshness=x.stale?"stale":"observed";list.append(b);
 }
 if(!children.length&&!v.item)list.append(el("p",available?(query?"Không có kết quả phù hợp.":"Chưa có quan sát cho cấp này."):"Chưa có dữ liệu.","cc-muted"));
 find(".cc-page").textContent=children.length?((page*size+1)+"–"+Math.min(children.length,(page+1)*size)+" / "+children.length):"0";
 find(".cc-prev").disabled=page===0;find(".cc-next").disabled=(page+1)*size>=children.length;
 find(".cc-up").disabled=v.level===0;
 const detail=find(".cc-detail");detail.replaceChildren(el("h3",v.title),el("p","Sức khỏe chức năng: Chưa xác minh","cc-unknown"));
 if(v.item){
  const r=v.item.row;
  for(const [k,val] of [["Dự án",v.p.label],["Nguồn",labels[r.source]],["Node",r.node],["Trạng thái quan sát",r.state],["Phiên bản",r.version],["Vị trí",r.location],["Thời điểm",fmt(r.at)],["Độ mới",r.fresh===true?"Trong 15 phút tại lần đo":"Cũ / chưa xác minh"]]){
   const pair=el("div",undefined,"cc-pair");pair.append(el("small",k),el("span",clean(val)||"Chưa xác minh"));detail.append(pair);
  }
 }else{
  detail.append(el("p",v.p?v.p.count+" bản ghi quan sát trong dự án":projects.length+" nhóm dự án được quan sát"));
  detail.append(el("p","Chạm nhánh để đi sâu. Đây là cây kiểm kê, không phải quan hệ phụ thuộc mã nguồn.","cc-muted"));
 }
 detail.append(el("p","Module chức năng, agent, task, rủi ro và nghiệm thu: chưa có ánh xạ xác minh ở lát cắt này.","cc-muted"));
 if(v.p&&v.p.id){const a=el("a","Mở hồ sơ Quản gia","cc-link");a.href="/steward?"+new URLSearchParams({project:v.p.id,intent:"now"});detail.append(a);}
 const tree=find(".cc-tree");tree.replaceChildren(button("Toàn hệ thống",()=>setPath([]),"cc-tree-root"));
 for(const p of projects.slice(0,100)){const b=button(p.label,()=>setPath([p.id]));b.setAttribute("aria-current",v.p&&v.p.id===p.id?"true":"false");tree.append(b);}
 if(projects.length>100)tree.append(el("small","Tìm các dự án còn lại trên canvas."));
 find(".cc-status").textContent=loading?"Đang tải…":available?"Nguồn: Quản gia · "+fmt(asof):"Chưa có dữ liệu được xác minh";
}
async function load(){
 if(loading)return;loading=true;find(".cc-reload").disabled=true;render();
 try{const r=await fetch("/api/steward?intent=now",{credentials:"same-origin",cache:"no-store",headers:{Accept:"application/json"}});
 if(!r.ok)throw new Error("HTTP "+r.status);
 const data=await r.json();if(!data||!Array.isArray(data.projects)||!Array.isArray(data.items))throw new Error("Dữ liệu không hợp lệ");
 projects=index(data);asof=data.at;available=true;path=path.slice(0,view(projects,path).level);find(".cc-error").textContent="";
 }catch(e){projects=[];path=[];available=false;asof=null;find(".cc-error").textContent="Không tải được bản đồ riêng tư ("+clean(e.message)+"). Không hiển thị dữ liệu cũ như dữ liệu mới.";}
 finally{loading=false;find(".cc-reload").disabled=false;render();}
}
find(".cc-search").addEventListener("input",e=>{query=e.target.value;page=0;render();});
find(".cc-up").addEventListener("click",()=>setPath(path.slice(0,-1)));
find(".cc-prev").addEventListener("click",()=>{page--;render();});
find(".cc-next").addEventListener("click",()=>{page++;render();});
find(".cc-reload").addEventListener("click",load);
find(".cc-expand").addEventListener("click",async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else if(host.requestFullscreen)await host.requestFullscreen();else host.classList.toggle("cc-expanded");}catch{host.classList.toggle("cc-expanded");}});
load();
})();