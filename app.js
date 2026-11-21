"use strict";
(() => {
const flows={
normal:{title:"Hoàn thiện màn hình bán hàng",steps:[
["QUEUED","Task đang chờ","Chưa giao agent và chưa có bằng chứng chạy.",0,0,"CHỜ TASK","CHỜ REVIEW","CHƯA XÁC MINH","Task được tạo","Task record · chưa có phiên",0],
["DISPATCHED","Đã gửi yêu cầu","Giao prompt thành công không đồng nghĩa đang chạy.",0,9,"ĐÃ GỬI","CHỜ REVIEW","ĐỢI BẰNG CHỨNG","Prompt được gửi","Biên nhận gửi, chưa có tiến độ",0],
["RUNNING_VERIFIED","Claude đang viết code","Có hoạt động code được quan sát. % nghiệm thu chưa tăng.",0,31,"ĐANG CHẠY","CHỜ REVIEW","THEO DÕI","Agent có tiến độ","LOCAL_OBSERVED · output mới",1],
["TESTING","Kiểm thử đã đạt","Test PASS là một cổng bằng chứng, chưa phải task đã xong.",1,52,"ĐÃ COMMIT","CHỜ REVIEW","1/3 CỔNG PASS","Kiểm thử tự động PASS","AUTOMATED_TESTS · commit có bằng chứng",1],
["REVIEW","Codex đang review","Review độc lập đã PASS; vẫn chờ cổng nghiệm thu cuối.",2,76,"CODE ĐÃ COMMIT","REVIEW PASS","2/3 CỔNG PASS","Review độc lập PASS","QA reviewer xác nhận",1],
["ACCEPTED","Đã nghiệm thu trong demo","Ba cổng đạt. Deploy là một mốc khác, không tự suy diễn.",3,88,"CODE HOÀN TẤT","REVIEW PASS","3/3 CỔNG PASS","Nghiệm thu độc lập","ACCEPTED · chỉ dữ liệu mẫu",1]
]},
stuck:{title:"Sửa lỗi nhập liệu POS",steps:[
["QUEUED","Task đang chờ","Chưa có phiên agent.",0,0,"CHỜ TASK","CHỜ REVIEW","CHƯA XÁC MINH","Task được tạo","Không có hoạt động",0],
["DISPATCHED","Đã giao Claude","Chỉ có biên nhận gửi, chưa có bằng chứng chạy.",0,12,"ĐÃ GỬI","CHỜ REVIEW","ĐỢI BẰNG CHỨNG","Prompt được gửi","Dispatch ACK không phải RUNNING",0],
["RUNNING_VERIFIED","Agent đang làm code","Đã có tiến độ thực thi được quan sát.",0,30,"ĐANG CHẠY","CHỜ REVIEW","CHƯA CÓ TEST","Code được cập nhật","Có output mới được xác minh",1],
["WAITING_INPUT","Agent chờ nhập liệu","Có yêu cầu xác nhận nên tiến độ dừng.",0,52,"CHỜ INPUT","CHỜ REVIEW","CHƯA CÓ PASS","Phát hiện chờ nhập liệu","Prompt đang chờ trả lời",0],
["STALLED","Không có tiến độ mới","Heartbeat còn nhưng không có output mới.",0,77,"STALLED","CHỜ REVIEW","CHƯA CÓ PASS","Task bị kẹt","Không tăng % vì heartbeat",0],
["BLOCKED","Cần xử lý blocker","Không tự chạy lại hay vượt quyền. Task dừng tại đây.",0,100,"BLOCKED","CHỜ REVIEW","0/3 CỔNG PASS","TTL đã hết","Cần người xử lý; chưa nghiệm thu",0]
]},
offline:{title:"Đồng bộ dữ liệu kiosk",steps:[
["QUEUED","Task đang chờ","Chưa bắt đầu chạy.",0,0,"CHỜ TASK","CHỜ REVIEW","CHƯA XÁC MINH","Task được tạo","Đang nằm trong hàng đợi",0],
["DISPATCHED","Đã gửi yêu cầu","Giao thành công, chưa xác minh thực thi.",0,11,"ĐÃ GỬI","CHỜ REVIEW","ĐỢI BẰNG CHỨNG","Đã giao task","Có biên nhận gửi",0],
["RUNNING_VERIFIED","Node đang gửi tiến độ","Có output mới với bằng chứng thời gian.",0,33,"ĐANG CHẠY","CHỜ REVIEW","CHƯA CÓ TEST","Quan sát hoạt động","LOCAL_OBSERVED từ node",1],
["CONNECTION_LOST","Node mất kết nối","Không biết agent đã tiếp tục hay dừng.",0,52,"MẤT KẾT NỐI","CHỜ REVIEW","KHÔNG LIÊN LẠC","Mất kết nối node","Không suy diễn còn chạy",0],
["STALE","Bằng chứng đã cũ","Nguồn cũ không chứng minh trạng thái hiện tại.",0,73,"STALE","CHỜ REVIEW","BẰNG CHỨNG CŨ","Quá hạn freshness","Heartbeat cũ không chứng minh tiến độ",0],
["UNKNOWN","Không rõ trạng thái thực","Đợi dữ liệu mới, không tự tuyên bố PASS hoặc FAIL.",0,90,"UNKNOWN","CHỜ REVIEW","ĐỢI XÁC MINH","Không xác minh được nguồn","UNKNOWN, không được tự nghiệm thu",0]
]}
};
const el=id=>document.getElementById(id),txt=(id,v)=>el(id).textContent=String(v);
const a={scenario:"normal",idx:0,playing:false,speed:1,timer:null};
const labels=["Chờ","Gửi","Code","Test","Review","Xong"];
function draw(){
const data=flows[a.scenario],s=data.steps[a.idx];
txt("task-name",data.title);txt("step-counter","Bước "+(a.idx+1)+"/6");
txt("status-code",s[0]);txt("status-title",s[1]);txt("status-detail",s[2]);
txt("gate-value",Math.round(s[3]*100/3)+"%");txt("gate-detail",s[3]+"/3 cổng bằng chứng");
txt("ttl-value",s[4]+"%");txt("ttl-detail",Math.round(s[4]*.3)+" / 30 phút · ví dụ");
txt("claude",s[5]);txt("codex",s[6]);txt("harness",s[7]);txt("evidence",s[9]);
const warning=/WAITING|STALLED|BLOCKED|LOST|STALE|UNKNOWN/.test(s[0]);
el("workflow").dataset.mode=warning?"warning":s[0]==="ACCEPTED"?"accepted":"normal";
el("flow-bar").style.width=((a.idx+1)*100/6)+"%";
el("gate-bar").style.width=(s[3]*100/3)+"%";el("ttl-bar").style.width=s[4]+"%";
el("steps").replaceChildren();
labels.forEach((name,i)=>{const e=document.createElement("span");e.textContent=name;e.className=i<=a.idx?"on":"";if(i===a.idx)e.setAttribute("aria-current","step");el("steps").appendChild(e)});
el("events").replaceChildren();
for(let n=a.idx;n>=0;n--){const step=data.steps[n],li=document.createElement("li"),div=document.createElement("div"),head=document.createElement("strong"),desc=document.createElement("p"),time=document.createElement("time");
head.textContent=step[8];desc.textContent=step[9];time.textContent="+"+n*4+"s";div.append(head,desc);li.append(div,time);el("events").appendChild(li)}
txt("observed",s[10]?"Đã quan sát thực thi":"Chưa có bằng chứng mới");
el("next").disabled=a.idx===5;
}
function stop(){clearTimeout(a.timer);a.timer=null;a.playing=false;txt("play","▶ Tiếp tục");el("play").setAttribute("aria-pressed","false")}
function tick(){if(!a.playing)return;if(a.idx>=5){stop();return}a.timer=setTimeout(()=>{a.idx++;draw();tick()},a.speed===1?4000:2000)}
function play(){if(a.idx>=5)a.idx=0;a.playing=true;txt("play","❚❚ Tạm dừng");el("play").setAttribute("aria-pressed","true");draw();tick()}
function select(id){if(!Object.prototype.hasOwnProperty.call(flows,id))return;stop();a.scenario=id;a.idx=0;document.querySelectorAll("[data-scenario]").forEach(b=>{const active=b.dataset.scenario===id;b.setAttribute("aria-pressed",String(active));b.classList.toggle("selected",active)});draw();play()}
el("play").addEventListener("click",()=>a.playing?stop():play());
el("next").addEventListener("click",()=>{stop();a.idx=Math.min(5,a.idx+1);draw()});
el("reset").addEventListener("click",()=>{stop();a.idx=0;draw()});
el("speed").addEventListener("click",()=>{a.speed=a.speed===1?2:1;txt("speed",a.speed+"× tốc độ");if(a.playing){clearTimeout(a.timer);tick()}});
document.querySelectorAll("[data-scenario]").forEach(b=>b.addEventListener("click",()=>select(b.dataset.scenario)));
document.addEventListener("visibilitychange",()=>{if(document.hidden)stop()});
select("normal");
})();