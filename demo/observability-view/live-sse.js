"use strict";
/* Public read-only fleet display. No commands, user credentials, or raw logs. */
(() => {
  const STREAM="https://terminal-bootstrap.mesflow.net/projectflow-live/events";
  const DIRECT="https://terminal-bootstrap.mesflow.net/projectflow-live/snapshot";
  const ARCHIVE="https://raw.githubusercontent.com/hungtranbkit/projectflow-workspace-manager/live-data/live-status.json";
  const $=id=>document.getElementById(id);
  const set=(id,value)=>{const e=$(id);if(e)e.textContent=String(value)};
  const int=x=>Number.isSafeInteger(x)&&x>=0&&x<=100000;
  const publicNodeNames=new Set(["dell-5530","dell-linux","hp-linux","local","m910",
    "mac17-macbook-pro","macbook","nec1","win"]);
  const isNodeLabel=value=>typeof value==="string" &&
    (/^Node \d{2}$/.test(value)||publicNodeNames.has(value));
  const labels={online:"Online",degraded:"Degraded",offline:"Offline",unknown:"Unknown"};
  const caps={healthy:"Khỏe",overloaded:"Quá tải",draining:"Đang drain",unknown:"Chưa rõ"};
  const agents={claude:"Claude",codex:"Codex",shell:"Shell",other:"Khác"};
  const activity={under_1m:"Mới trong 1 phút", "1_to_5m":"1–5 phút trước",over_5m:"Trên 5 phút",unknown:"Chưa có thời điểm"};
  const publicActivityLabels=new Set([
    "Có dòng báo đang chạy kiểm thử",
    "Có dòng lệnh kiểm thử Python",
    "Có dòng lệnh kiểm thử JavaScript",
    "Có dòng báo thu thập bài kiểm thử",
    "Có dòng báo kết quả kiểm thử PASS",
    "Có dòng báo kiểm thử thất bại",
    "Có dòng báo build hoặc biên dịch",
    "Có dòng báo thao tác chỉnh sửa file",
    "Có dòng báo đọc hoặc phân tích mã nguồn",
    "Có dòng báo thực thi công cụ",
    "Có dòng thông báo lỗi cần kiểm tra"
  ]);
  const execNames={RUNNING_VERIFIED:"RUNNING · có bằng chứng",IDLE:"IDLE",WAITING_INPUT:"Chờ nhập liệu",BLOCKED:"Bị chặn",EXITED:"Đã dừng",STALLED:"Bị kẹt",OPEN_UNVERIFIED:"MỞ · chưa xác minh"};
  const projects=new Set(["Zalo Group Hub","MESFlow","ESP32 Kiosk / OTA","ProjectFlow","Brand Agent","Screen Remote","RemoteOps","Chưa phân loại"]);
  let activeProject="all";
  let activeState="all";
  let displayLimit=24;
  const stateRank={RUNNING_VERIFIED:0,WAITING_INPUT:1,BLOCKED:2,STALLED:3,IDLE:4,OPEN_UNVERIFIED:5,EXITED:6};
  const stateFilters=[["all","Tất cả"],["running","RUNNING"],["waiting","Chờ nhập liệu"],["blocked","Bị chặn"],["idle","IDLE"],["unverified","Chưa xác minh"]];
  const stateMatch=(s)=>activeState==="all"||
    (activeState==="running"&&s.execution_state==="RUNNING_VERIFIED")||
    (activeState==="waiting"&&s.execution_state==="WAITING_INPUT")||
    (activeState==="blocked"&&["BLOCKED","STALLED"].includes(s.execution_state))||
    (activeState==="idle"&&s.execution_state==="IDLE")||
    (activeState==="unverified"&&["OPEN_UNVERIFIED","EXITED"].includes(s.execution_state));
  const st={last:null,lastStreamAt:0,lastRenderedAt:0,source:"none",archiveBusy:false,stream:null,previous:null,events:[]};
  function validate(d){
    if(!d||![1,2].includes(d.schema_version)||d.kind!=="terminal-mcp-public-aggregate")return false;
    if(!d.nodes||!Array.isArray(d.nodes.items)||!d.sessions||!d.acceptance||typeof d.observed_at!=="string")return false;
    if(!Number.isFinite(Date.parse(d.observed_at))||d.nodes.items.length>25)return false;
    for(const name of ["total","online","offline","unknown"])if(!int(d.nodes[name]))return false;
    const degraded=d.schema_version===2?d.nodes.degraded:0;
    if(!int(degraded)||d.nodes.total!==d.nodes.online+d.nodes.offline+d.nodes.unknown+degraded||d.nodes.items.length!==d.nodes.total)return false;
    if(!int(d.sessions.open)||!int(d.sessions.recent_activity_signals)||d.sessions.recent_activity_signals>d.sessions.open)return false;
    if(d.sessions.running_verified!==null||d.acceptance.percent!==null||d.acceptance.ttl_percent!==null)return false;
    if(d.schema_version===2&&(!int(d.sessions.recent_last_minute)||!Array.isArray(d.sessions.items)||d.sessions.items.length>100||
      !Array.isArray(d.sessions.projects)||d.sessions.projects.length>12||!int(d.sessions.proven_running)))return false;
    const allowed=["online","offline","unknown","degraded"];
    if(!d.nodes.items.every(n=>isNodeLabel(n.label)&&allowed.includes(n.status)&&int(n.open_sessions)&&int(n.recent_activity_signals)))return false;
    if(d.schema_version===2){
      for(const s of d.sessions.items){
        if(!/^Session \d{2,3}$/.test(s.label)||!isNodeLabel(s.node)||!Object.hasOwn(agents,s.agent_type)||
           !Object.hasOwn(activity,s.activity_bucket)||!Object.hasOwn(execNames,s.execution_state)||
           !projects.has(s.project)||typeof s.task_hint!=="string"||s.task_hint.length>120||
           !["session_name_hint","unavailable","trusted_task_label"].includes(s.task_source)||
           !["registry_only","verified_recent_checkpoint","node_observed","terminal_output_delta","terminal_screen_delta","agent_ui_busy"].includes(s.evidence)||
           !(s.running_verified===null||(s.running_verified===true&&s.execution_state==="RUNNING_VERIFIED"))||
           (s.commit_ref!==null&&!/^[a-f0-9]{7,12}$/.test(s.commit_ref))||
           (s.branch_ref!==null&&!/^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,69}$/.test(s.branch_ref))||
           (s.session!==null&&!(typeof s.session==="string"&&/^[a-z0-9][a-z0-9._-]{2,89}$/.test(s.session)))||
           (s.recent_actions!==undefined&&(!Array.isArray(s.recent_actions)||
             s.recent_actions.length>3||
             !s.recent_actions.every(x=>typeof x==="string"&&publicActivityLabels.has(x))||
             (s.running_verified!==true&&s.recent_actions.length>0))))return false;
      }
      if(!d.sessions.projects.every(p=>projects.has(p.name)&&int(p.open_sessions)))return false;
      if(d.sessions.proven_running>d.sessions.open)return false;
      if(d.sessions.task_label_verified!==undefined&&
         (!int(d.sessions.task_label_verified)||d.sessions.task_label_verified>d.sessions.open))return false;
      if(d.sessions.agent_identified!==undefined&&
         (!int(d.sessions.agent_identified)||d.sessions.agent_identified>d.sessions.open))return false;
      if(d.sessions.displayed_count!==undefined&&d.sessions.displayed_count!==d.sessions.items.length)return false;
      if(d.sessions.hidden_count!==undefined&&!int(d.sessions.hidden_count))return false;
      if(d.sessions.coverage!==undefined&&d.sessions.coverage!==null){
        const c=d.sessions.coverage;
        if(!c||!Array.isArray(c.covered_nodes)||!Array.isArray(c.unreachable_nodes)||
           c.covered_nodes.length>25||c.unreachable_nodes.length>25||
           !c.covered_nodes.every(isNodeLabel)||!c.unreachable_nodes.every(isNodeLabel))return false;
      }
    }
    return true;
  }
  const format=(iso)=>new Intl.DateTimeFormat("vi-VN",{timeZone:"Asia/Ho_Chi_Minh",hour:"2-digit",minute:"2-digit",second:"2-digit",day:"2-digit",month:"2-digit"}).format(new Date(iso));
  function element(tag,cl,content){
    const e=document.createElement(tag);if(cl)e.className=cl;if(content!==undefined)e.textContent=String(content);return e;
  }
  function statusUnknown(reason){
    $("live-panel").dataset.health="unknown";set("live-health","UNKNOWN · Chưa xác minh");set("live-time","Không có bản ghi hợp lệ");
    for(const id of ["live-nodes","live-open","live-signal","live-one-minute"])set(id,"—");
    set("live-acceptance","UNKNOWN");set("live-ttl","UNKNOWN");set("live-transport","Chưa kết nối");
    set("live-message",reason||"Không suy diễn dữ liệu nếu nguồn không hợp lệ.");
    set("live-running-count","—");set("live-running-note","Không có dữ liệu được xác minh");set("live-task-coverage","Nhãn task và agent chưa xác minh.");
    const states=$("live-state-filters");if(states)states.replaceChildren();
    const more=$("live-show-more");if(more)more.hidden=true;
    $("live-node-list").replaceChildren();$("live-sessions").replaceChildren();$("live-changes").replaceChildren();$("live-projects").replaceChildren();set("live-sessions-note","Không có dữ liệu phiên xác minh.");
  }
  function noteChange(d){
    const before=st.previous;
    if(before){
      const m=[];
      if(before.sessions.open!==d.sessions.open)m.push("Phiên mở: "+before.sessions.open+" → "+d.sessions.open);
      if(before.nodes.online!==d.nodes.online)m.push("Node online: "+before.nodes.online+" → "+d.nodes.online);
      if(before.nodes.offline!==d.nodes.offline)m.push("Node offline: "+before.nodes.offline+" → "+d.nodes.offline);
      if(m.length)st.events.unshift({at:d.observed_at,text:m.join(" · ")});
    }
    st.previous=d;st.events=st.events.slice(0,6);
    const list=$("live-changes");list.replaceChildren();
    for(const entry of st.events){
      const li=element("li"),detail=element("span","",entry.text),time=element("time","",format(entry.at));
      li.append(detail,time);list.appendChild(li);
    }
    if(!st.events.length)list.appendChild(element("li","", "Chưa thấy biến động tổng hợp từ lúc mở trang."));
  }
  function render(d,source){
    if(!validate(d)){statusUnknown("Nguồn SSE trả dữ liệu không hợp lệ. Không giữ trạng thái LIVE cũ.");return false;}
    const age=(Date.now()-Date.parse(d.observed_at))/1000;
    if(!Number.isFinite(age)||age < -120||age>900){
      statusUnknown("Bản ghi quá cũ hoặc sai thời gian. Không hiển thị số liệu như đang hoạt động.");
      st.source="stale";
      return false;
    }
    const stream=source==="sse",fallback=source==="direct";
    st.source=source;st.last=d;st.lastRenderedAt=Date.now();
    $("live-panel").dataset.health=stream?"fresh":"stale";
    set("live-health",stream?"LIVE · Có luồng cập nhật trực tiếp":fallback?"DỮ LIỆU NHANH · Đang dự phòng":"BẢN LƯU · Không realtime");
    set("live-time","Xác minh lúc "+format(d.observed_at));
    set("live-nodes",d.nodes.online+" / "+d.nodes.total);
    set("live-open",d.sessions.open);
    set("live-signal",d.sessions.recent_activity_signals);
    set("live-one-minute",d.schema_version===2?d.sessions.recent_last_minute:"—");
    set("live-acceptance","UNKNOWN");set("live-ttl","UNKNOWN");
    set("live-transport",stream?"SSE · khoảng 3 giây":fallback?"HTTP · khoảng 15 giây":"GitHub archive · có cache");
    const coverage=d.sessions.coverage;
    const coverageText=coverage&&coverage.covered_nodes.length
      ?" Đang xác minh "+coverage.covered_nodes.length+" node, "+
       coverage.unreachable_nodes.length+" node chưa liên lạc."
      :"";
    set("live-message",stream?
      "Dữ liệu thật đang nhận qua SSE."+coverageText+
      " Chỉ báo RUNNING khi có output thực thi mới, không suy từ heartbeat.":
      "Luồng trực tiếp chưa được xác minh; đây là bản dự phòng. Không suy diễn RUNNING hay tiến độ nghiệm thu từ hoạt động phiên.");
    const grid=$("live-node-list");grid.replaceChildren();
    for(const n of d.nodes.items){
      const li=element("li"),head=element("strong","",n.label),pill=element("span","live-pill "+n.status,labels[n.status]);
      const details=element("span","live-node-detail",
        n.open_sessions+" phiên · "+n.recent_activity_signals+" tín hiệu ≤5 phút");
      li.append(head,pill,details);
      if(d.schema_version===2){
        li.appendChild(element("span","live-node-detail",
          "Sức tải: "+(caps[n.capacity]||"Chưa rõ")+" · tín hiệu ≤1 phút: "+n.signals_last_minute));
        const types=n.agent_types||{};
        li.appendChild(element("span","live-node-detail",
          "Claude "+(types.claude||0)+" · Codex "+(types.codex||0)+" · Shell "+(types.shell||0)));
      }
      grid.appendChild(li);
    }
    const list=$("live-sessions");list.replaceChildren();
    const projectList=$("live-projects");projectList.replaceChildren();
    if(d.schema_version===2){
      const all=[...d.sessions.items].sort((a,b)=>
        (stateRank[a.execution_state]??9)-(stateRank[b.execution_state]??9)||
        String(a.label).localeCompare(String(b.label)));
      set("live-running-count",d.sessions.proven_running);
      set("live-task-coverage",int(d.sessions.task_label_verified)&&int(d.sessions.agent_identified)
        ?"Task có nhãn chính thức: "+d.sessions.task_label_verified+"/"+d.sessions.open+
         " · Loại agent xác định: "+d.sessions.agent_identified+"/"+d.sessions.open
        :"Đang chờ số liệu xác minh nguồn task và agent.");
      set("live-running-note",d.sessions.proven_running
        ?"Có bằng chứng hoạt động mới; chưa đồng nghĩa đã sửa code":"Chưa có phiên RUNNING được xác minh từ các node đang kết nối");
      const stateList=$("live-state-filters");
      if(stateList){
        stateList.replaceChildren();
        for(const [key,label] of stateFilters){
          const count=key==="running"?d.sessions.proven_running:
            all.filter(s=>key==="all"||
              (key==="waiting"&&s.execution_state==="WAITING_INPUT")||
              (key==="blocked"&&["BLOCKED","STALLED"].includes(s.execution_state))||
              (key==="idle"&&s.execution_state==="IDLE")||
              (key==="unverified"&&["OPEN_UNVERIFIED","EXITED"].includes(s.execution_state))).length;
          const btn=element("button","live-state-filter"+(activeState===key?" selected":""),label+" · "+count);
          btn.type="button";btn.setAttribute("aria-pressed",String(activeState===key));
          btn.addEventListener("click",()=>{activeState=key;displayLimit=24;render(d,source)});
          stateList.appendChild(btn);
        }
      }
      const title=element("span","", "Dự án · "+d.sessions.projects.length+" nhóm");
      projectList.appendChild(title);
      for(const p of d.sessions.projects){
        const btn=element("button","live-project-filter"+(activeProject===p.name?" selected":""),
          p.name+" · "+p.open_sessions);
        btn.type="button";
        btn.setAttribute("aria-pressed",String(activeProject===p.name));
        btn.addEventListener("click",()=>{activeProject=activeProject===p.name?"all":p.name;displayLimit=24;render(d,source)});
        projectList.appendChild(btn);
      }
      const matches=all.filter(s=>
        (activeProject==="all"||activeProject===s.project)&&stateMatch(s));
      const visible=matches.slice(0,displayLimit);
      const showing=visible.length;
      for(const s of visible){
        const li=element("li","session-card"+(s.execution_state==="RUNNING_VERIFIED"?" running-first":""));
        const line=element("div","session-card-heading");
        line.appendChild(element("strong","",s.session||s.label));
        line.appendChild(element("span","session-state "+(s.execution_state==="RUNNING_VERIFIED"?"verified":"unverified"),
          execNames[s.execution_state]));
        li.appendChild(line);
        li.appendChild(element("div","session-context",s.project+" · "+s.node+" · "+agents[s.agent_type]));
        li.appendChild(element("p","session-work",s.task_source==="trusted_task_label"
          ?(s.running_verified===true?"Đang thực thi: ":"Task được gắn gần nhất: ")+s.task_hint
          :s.task_source==="session_name_hint"
          ?"Task gợi ý theo tên phiên: "+s.task_hint
          :"Chưa có task được xác minh"));
        li.appendChild(element("small","session-source",
          (s.task_source==="trusted_task_label"?"Nhãn task gắn qua Terminal MCP":s.task_source==="session_name_hint"?"Mục tiêu suy ra từ tên session":"Chưa có mô tả task")
          +" · tín hiệu: "+activity[s.activity_bucket]));
        if(s.commit_ref||s.branch_ref)li.appendChild(element("small","session-code",
          "Git tham chiếu: "+(s.branch_ref||"không rõ nhánh")
          +(s.commit_ref?" @ "+s.commit_ref:"")+
          " · chưa xác minh commit mới"));
        li.appendChild(element("small","session-proof",
          s.evidence==="terminal_output_delta"?"Đã quan sát nội dung terminal thay đổi khi tiến trình chạy · chưa xác minh sửa code":
          s.evidence==="terminal_screen_delta"?"Toàn màn hình terminal thay đổi khi tiến trình foreground chạy · không đồng nghĩa sửa code":
          s.evidence==="agent_ui_busy"?"Claude/Codex hiển thị đang xử lý · chưa xác minh thay đổi code":
          s.execution_state==="IDLE"?"Đã xác minh agent đang chờ lệnh":
          s.execution_state==="WAITING_INPUT"?"Agent đang chờ nhập liệu":
          s.running_verified===true?"RUNNING được xác minh bằng checkpoint mới":"Chưa có bằng chứng code mới / chưa xác minh RUNNING"));
        if(s.running_verified===true&&Array.isArray(s.recent_actions)&&s.recent_actions.length){
          const hints=element("div","session-activity-hints");
          hints.appendChild(element("small","session-activity-title",
            "Tín hiệu trong 8 dòng cuối · đã lọc, chưa xác minh kết quả"));
          for(const hint of s.recent_actions){
            hints.appendChild(element("small","session-activity-line","• "+hint));
          }
          li.appendChild(hints);
        }
        if(s.running_verified===true&&typeof s.last_checkpoint_at==="string"&&
           Number.isFinite(Date.parse(s.last_checkpoint_at))){
          li.appendChild(element("small","session-proof-time",
            "Bằng chứng hoạt động: "+format(s.last_checkpoint_at)+" · chưa suy diễn code/test/deploy"));
        }
        list.appendChild(li);
      }
      const more=$("live-show-more");
      if(more){
        more.hidden=showing>=matches.length;
        more.textContent="Xem thêm "+Math.min(24,matches.length-showing)+" phiên";
        more.onclick=()=>{displayLimit+=24;render(d,source)};
      }
      const scoped=activeProject==="all"&&activeState==="all"
        ?d.sessions.open+" phiên mở":matches.length+" phiên khớp bộ lọc";
      const incomplete=coverage&&coverage.unreachable_nodes.length
        ?" · "+coverage.unreachable_nodes.length+" node chưa kết nối":"";
      set("live-sessions-note","Đang hiển thị "+showing+"/"+scoped+
        incomplete+". RUNNING dựa trên output thực thi mới; mục tiêu suy từ tên phiên chỉ là gợi ý.");
    }else{
      projectList.appendChild(element("span","","Bản lưu chưa có tên dự án/task."));
      list.appendChild(element("li","","Bản lưu chưa có chi tiết phiên. Chỉ đang hiển thị tổng hợp."));
      set("live-sessions-note","Dữ liệu chi tiết đang chờ SSE.");
    }
    noteChange(d);
    return true;
  }
  async function directFallback(){
    if(st.archiveBusy)return;st.archiveBusy=true;
    try{
      for(const [url,mode] of [[DIRECT,"direct"],[ARCHIVE,"archive"]]){
        try{
          const response=await fetch(url,{cache:"no-store",credentials:"omit"});
          if(!response.ok)continue;
          const d=await response.json();
          if(Date.now()-st.lastStreamAt<12000)return;
          if(render(d,mode))return;
        }catch(_){/* Try next trusted public source. */}
      }
      if(Date.now()-st.lastStreamAt>=12000)statusUnknown("Không có nguồn LIVE hoặc dự phòng hợp lệ.");
    }finally{st.archiveBusy=false}
  }
  function connect(){
    if(!("EventSource" in window)){directFallback();return;}
    try{
      const stream=new EventSource(STREAM,{withCredentials:false});
      st.stream=stream;
      stream.addEventListener("status",ev=>{
        try{
          const d=JSON.parse(ev.data);
          if(render(d,"sse"))st.lastStreamAt=Date.now();
        }catch(_){statusUnknown("SSE trả dữ liệu không hợp lệ.")}
      });
      stream.addEventListener("unavailable",()=>{statusUnknown("Nguồn registry chưa sẵn sàng.");directFallback()});
      stream.onerror=()=>{if(Date.now()-st.lastStreamAt>=12000)directFallback()};
    }catch(_){directFallback()}
  }
  $("live-refresh").addEventListener("click",()=>{if(Date.now()-st.lastStreamAt>=12000)directFallback()});
  document.addEventListener("visibilitychange",()=>{if(!document.hidden && Date.now()-st.lastStreamAt>=12000)directFallback()});
  connect();
  setInterval(()=>{if(document.hidden)return;if(Date.now()-st.lastStreamAt>12000)directFallback()},15000);
})();