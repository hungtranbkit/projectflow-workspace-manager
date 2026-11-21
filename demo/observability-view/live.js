"use strict";
// Public, aggregate-only Terminal MCP snapshot. No private API, no control.
(() => {
  const $ = id => document.getElementById(id);
  const text = (id, value) => { const e = $(id); if (e) e.textContent = String(value); };
  const validInt = x => Number.isSafeInteger(x) && x >= 0 && x <= 100000;
  const stateNames = {online:"Đang kết nối",offline:"Mất kết nối",unknown:"Chưa xác minh"};
  const panels = {online:"online",offline:"offline",unknown:"unknown"};
  function invalid(reason) {
    text("live-health","Chưa có dữ liệu xác minh");
    text("live-time","Không xác định được thời điểm cập nhật");
    text("live-nodes","—");
    text("live-open","—");
    text("live-signal","—");
    text("live-acceptance","UNKNOWN");
    text("live-message", reason || "Không thể xác minh nguồn dữ liệu. Không dùng số liệu mô phỏng để thay thế.");
    $("live-panel").dataset.health="unknown";
    $("live-node-list").replaceChildren();
  }
  function validated(d){
    if(!d || d.schema_version!==1 || d.kind!=="terminal-mcp-public-aggregate")return false;
    if(!d.nodes || !d.sessions || !d.acceptance || !Array.isArray(d.nodes.items))return false;
    if(d.nodes.items.length>30)return false;
    for(const field of ["total","online","offline","unknown"])if(!validInt(d.nodes[field]))return false;
    if(d.nodes.total!==d.nodes.items.length || d.nodes.online+d.nodes.offline+d.nodes.unknown!==d.nodes.total)return false;
    for(const field of ["open","recent_activity_signals"])if(!validInt(d.sessions[field]))return false;
    if(d.sessions.recent_activity_signals>d.sessions.open || d.sessions.running_verified!==null)return false;
    if(d.acceptance.percent!==null || d.acceptance.ttl_percent!==null)return false;
    if(typeof d.observed_at!=="string" || !Number.isFinite(Date.parse(d.observed_at)))return false;
    return d.nodes.items.every(n=>typeof n.label==="string" && /^Node \d{2}$/.test(n.label)
      && ["online","offline","unknown"].includes(n.status)
      && validInt(n.open_sessions) && validInt(n.recent_activity_signals)
      && n.recent_activity_signals<=n.open_sessions);
  }
  function render(d){
    const instant = new Date(d.observed_at);
    const ageSec = (Date.now()-instant.getTime())/1000;
    const fresh=ageSec>=-120 && ageSec<=900;
    $("live-panel").dataset.health=fresh?"fresh":"stale";
    text("live-health",fresh?"LIVE · Dữ liệu Terminal MCP":"CẢNH BÁO · Dữ liệu đã cũ");
    text("live-time","Ghi nhận lúc "+new Intl.DateTimeFormat("vi-VN",{timeZone:"Asia/Ho_Chi_Minh",hour:"2-digit",minute:"2-digit",day:"2-digit",month:"2-digit"}).format(instant)+" (giờ Việt Nam)");
    text("live-nodes",d.nodes.online+" / "+d.nodes.total);
    text("live-open",d.sessions.open);
    text("live-signal",d.sessions.recent_activity_signals);
    text("live-acceptance","UNKNOWN");
    text("live-message",fresh
      ?"Số liệu thật từ registry Terminal MCP, cập nhật khoảng 5 phút/lần. Phiên mở và tín hiệu không chứng minh agent đang viết code hoặc task đã nghiệm thu."
      :"Bản ghi quá 15 phút hoặc sai thời gian. Các số bên trên chỉ là số liệu cũ; không suy diễn trạng thái hiện tại.");
    const list=$("live-node-list");list.replaceChildren();
    for(const n of d.nodes.items){
      const li=document.createElement("li"),label=document.createElement("strong"),pill=document.createElement("span"),count=document.createElement("span");
      label.textContent=n.label;
      pill.textContent=fresh?stateNames[n.status]:"Chưa cập nhật";
      pill.className="live-pill "+(fresh?panels[n.status]:"unknown");
      count.textContent=n.open_sessions+" phiên mở · "+n.recent_activity_signals+" tín hiệu mới";
      count.className="live-node-detail";
      li.append(label,pill,count);list.appendChild(li);
    }
  }
  async function update(){
    try{
      // Only same-origin, sanitized static JSON; no Terminal MCP client API.
      const response=await fetch(new URL("live-status.json",document.baseURI),{cache:"no-store",credentials:"omit"});
      if(!response.ok)throw Error("No published snapshot");
      const d=await response.json();
      if(!validated(d))throw Error("Invalid snapshot schema");
      render(d);
    }catch(_error){invalid("Chưa tải được bản ghi Terminal MCP hợp lệ. Không hiển thị số liệu giả thay thế.");}
  }
  $("live-refresh").addEventListener("click",update);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)update()});
  update();
  setInterval(()=>{if(!document.hidden)update()},60000);
})();