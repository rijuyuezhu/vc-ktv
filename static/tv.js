"use strict";
const video=document.getElementById("video");
const cover=document.getElementById("cover");
const status=document.getElementById("status");
const start=document.getElementById("start");
const error=document.getElementById("error");
const token=new URLSearchParams(location.search).get("token");
let activeId=null;
let started=false;
let inFlight=false;
let rendered=false;
async function api(path,method="GET",body=null){
  const options={method,cache:"no-store"};
  if(body){options.headers={"Content-Type":"application/json"};options.body=JSON.stringify(body);}
  const join=path.includes("?")?"&":"?";
  const response=await fetch(path+join+"token="+encodeURIComponent(token||""),options);
  const result=await response.json();
  if(!response.ok)throw Error(result.detail||"请求失败");
  return result;
}
function fail(e){error.textContent=e.message||String(e);status.textContent="连接异常，正在重试";}
async function next(){
  if(inFlight||!started||activeId!==null)return;
  inFlight=true;
  try{
    const answer=await api("/api/tv/next","POST");
    if(!answer.active){status.textContent="正在等待下一首歌……";cover.classList.remove("hidden");return;}
    activeId=answer.active.id;
    video.src=answer.active.media_url;
    status.textContent=answer.active.title+"  "+answer.active.nickname;
    cover.classList.add("hidden");
    try{await video.play();}catch(e){
      status.textContent="请点击画面以允许播放声音";
      cover.classList.remove("hidden");start.textContent="▶ 恢复播放";
    }
  }catch(e){fail(e);}finally{inFlight=false;}
}
async function complete(reason=""){
  if(activeId===null)return;
  const id=activeId;
  activeId=null;
  video.removeAttribute("src");
  video.load();
  try{await api("/api/tv/finish","POST",{song_id:id,reason});}
  catch(e){if(!String(e.message).includes("当前播放已变化"))fail(e);}
  await next();
}
video.addEventListener("ended",()=>{complete();});
video.addEventListener("error",()=>{if(activeId!==null){status.textContent="媒体无法播放，正在跳过这一首……";complete("skip");}});
start.onclick=async()=>{
  started=true;error.textContent="";
  if(activeId!==null){cover.classList.add("hidden");try{await video.play();}catch(e){fail(e);}return;}
  await next();
};
setInterval(async()=>{
  if(!started||inFlight||activeId!==null)return;
  await next();
},2000);
