"use strict";
// Static standalone experiment: absolutely no privileged VC-KTV API access.
const $=id=>document.getElementById(id);
const video=$("video");
const status=$("status");
let plan=null,byId=new Map(),blobUrl=null;
let stalls=0,stallStarted=null,stallMs=0;
function tell(message){status.textContent=message;}
function num(value,fallback=0){const n=Number(value);return Number.isFinite(n)?n:fallback;}
function clock(seconds){return Math.max(0,num(seconds)).toFixed(2);}
function attachTimeline(data,windows){
  if(!data||!Array.isArray(data.lines)||!windows||typeof windows!=="object")throw Error("无效时间轴或显示计划");
  plan=windows;byId=new Map(data.lines.map(line=>[line.id,line]));
  update();
}
async function fileJson(id){const file=$(id).files?.[0];return file?JSON.parse(await file.text()):null;}
async function loadTimeline(){
  const t=await fileJson("timeline-file");const p=await fileJson("plan-file");
  if((t&&!p)||(!t&&p))throw Error("需要同时提供 timeline.json 和 display_plan.json");
  if(t&&p)attachTimeline(t,p);
}
function appendSegment(parent,text,unit,t){
  if(!text)return;
  const span=document.createElement("span");span.textContent=text;
  if(Number.isFinite(unit?.start)&&Number.isFinite(unit?.end)&&unit.end>unit.start){
    const fraction=Math.max(0,Math.min(1,(t-unit.start)/(unit.end-unit.start)));
    span.className="timed";
    span.style.backgroundImage=`linear-gradient(90deg,#ffe773 ${100*fraction}%,#fff ${100*fraction}%)`;
  }
  parent.append(span);
}
function drawLine(slot,line,t){
  const target=$("line-"+slot);target.replaceChildren();
  if(!line)return;
  let pos=0;
  for(const unit of line.units||[]){
    const end=Math.max(pos,Math.min(line.text.length,num(unit.source_end,pos)));
    appendSegment(target,line.text.slice(pos,end),unit,t);
    pos=end;
  }
  if(pos<line.text.length)appendSegment(target,line.text.slice(pos),null,t);
}
function update(){
  const offset=Math.max(-60,Math.min(60,num($("offset").value)));
  const t=num(video.currentTime)-offset;
  const selected=[null,null];
  if(plan){
    for(const [id,window] of Object.entries(plan)){
      if(!(window.start<=t&&t<window.end))continue;
      const slot=window.slot;
      if((slot===0||slot===1)&&byId.has(id))selected[slot]=byId.get(id);
    }
  }
  drawLine(0,selected[0],t);drawLine(1,selected[1],t);
  return {time:t,lines:selected.map(x=>x?.text||"")};
}
function frame(){if(!video.paused&&!video.ended)update();requestAnimationFrame(frame);}
video.addEventListener("timeupdate",update);video.addEventListener("seeked",update);
video.addEventListener("error",()=>tell("媒体无法播放（可能是 CDN 防盗链、过期或编码不支持）。请使用本地成片回退。"));
video.addEventListener("waiting",()=>{stalls++;stallStarted=performance.now();});
video.addEventListener("playing",()=>{if(stallStarted!==null){stallMs+=performance.now()-stallStarted;stallStarted=null;}});
video.addEventListener("loadedmetadata",()=>tell(`媒体 ${video.videoWidth}×${video.videoHeight}，总时长 ${clock(video.duration)} 秒。`));
$("offset").addEventListener("input",update);
$("timeline-file").addEventListener("change",()=>loadTimeline().catch(e=>tell(e.message)));
$("plan-file").addEventListener("change",()=>loadTimeline().catch(e=>tell(e.message)));
$("load").onclick=()=>{
  if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;}
  const file=$("media-file").files?.[0];
  if(file){blobUrl=URL.createObjectURL(file);video.src=blobUrl;}
  else{video.src=$("media-url").value.trim();}
  video.load();stalls=0;stallMs=0;stallStarted=null;
};
// Read-only instrumentation for reproducible browser experiments.
window.overlaySnapshot=()=>({time:video.currentTime,duration:video.duration,width:video.videoWidth,height:video.videoHeight,stalls,stallMs,
  buffered:[...Array(video.buffered.length)].map((_,i)=>[video.buffered.start(i),video.buffered.end(i)]),
  overlay:update()});
requestAnimationFrame(frame);
