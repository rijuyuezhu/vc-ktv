"use strict";
let video=document.getElementById("video");
let standby=document.getElementById("preload");
const cover=document.getElementById("cover");
const status=document.getElementById("status");
const start=document.getElementById("start");
const error=document.getElementById("error");
const token=new URLSearchParams(location.search).get("token");
let activeId=null;
let started=false;
let inFlight=false;
let finishing=false;
let preloaded=null;
let preloadFailed=null;
let preloadInFlight=false;
let lastProbe=-Infinity;
async function api(path,method="GET",body=null){
  const options={method,cache:"no-store"};
  if(body){options.headers={"Content-Type":"application/json"};options.body=JSON.stringify(body);}
  const join=path.includes("?")?"&":"?";
  const response=await fetch(path+join+"token="+encodeURIComponent(token||""),options);
  const result=await response.json();
  if(!response.ok)throw Error(result.detail||"请求失败");
  return result;
}
function fail(e){
  const message=e.message||String(e);
  if(message.includes("手动投屏")) {
    error.textContent="";status.textContent="当前使用手动投屏，请在管理后台切歌";return;
  }
  error.textContent=message;status.textContent="连接异常，正在重试";
}
function clearPreload(){
  preloaded=null;
  if(standby.hasAttribute("src")){
    standby.removeAttribute("src");
    standby.load();
  }
}
function preloadKey(song){return song.id+":"+song.media_file;}
async function checkPreload(){
  if(!started||activeId===null||inFlight||preloadInFlight||!Number.isFinite(video.duration))return;
  const remaining=video.duration-video.currentTime;
  if(remaining>Math.min(60,video.duration/2)||remaining<=0)return;
  // Do not compete with an already-stalling current video for bandwidth.
  let buffered=0;
  for(let i=0;i<video.buffered.length;i++){
    if(video.buffered.start(i)<=video.currentTime&&video.currentTime<=video.buffered.end(i)){
      buffered=video.buffered.end(i)-video.currentTime;
      break;
    }
  }
  if(buffered<Math.min(10,remaining))return;
  const now=performance.now();
  if(now-lastProbe<8000)return;
  lastProbe=now;
  const playingId=activeId;
  preloadInFlight=true;
  try{
    const {candidate}=await api("/api/tv/preload");
    if(activeId!==playingId||!started)return;
    if(!candidate){clearPreload();return;}
    const key=preloadKey(candidate);
    if(preloaded&&preloadKey(preloaded)===key)return;
    clearPreload();
    if(preloadFailed===key)return;
    preloaded=candidate;
    standby.src=candidate.media_url;
    standby.load();
  }catch{
    // Speculative loading must never interrupt the current song.
  }finally{preloadInFlight=false;}
}
async function next(){
  if(inFlight||finishing||!started||activeId!==null)return;
  inFlight=true;
  try{
    const answer=await api("/api/tv/next","POST");
    if(!answer.active){status.textContent="正在等待下一首歌……";cover.classList.remove("hidden");return;}
    activeId=answer.active.id;
    if(preloaded&&preloadKey(preloaded)===preloadKey(answer.active)&&!standby.error){
      const old=video;
      video=standby;
      standby=old;
      video.classList.remove("preloading");
      standby.classList.add("preloading");
      preloaded=null;
    }else{
      clearPreload();
      video.src=answer.active.media_url;
    }
    preloadFailed=null;
    lastProbe=-Infinity;
    status.textContent=answer.active.title+"  "+answer.active.nickname;
    cover.classList.add("hidden");
    try{await video.play();}catch(e){
      status.textContent="请点击画面以允许播放声音";
      cover.classList.remove("hidden");start.textContent="▶ 恢复播放";
    }
  }catch(e){fail(e);}finally{inFlight=false;}
}
async function complete(reason=""){
  if(activeId===null||finishing)return;
  finishing=true;
  const id=activeId;
  activeId=null;
  video.removeAttribute("src");
  video.load();
  try{await api("/api/tv/finish","POST",{song_id:id,reason});}
  catch(e){if(!String(e.message).includes("当前播放已变化"))fail(e);}
  finally{finishing=false;}
  await next();
}
for(const element of [video,standby]){
  element.addEventListener("ended",()=>{if(element===video)complete();});
  element.addEventListener("error",()=>{
    if(element!==video){
      if(preloaded)preloadFailed=preloadKey(preloaded);
      clearPreload();
      return;
    }
    if(activeId!==null){status.textContent="媒体无法播放，正在跳过这一首……";complete("skip");}
  });
  element.addEventListener("timeupdate",()=>{if(element===video)checkPreload();});
}
start.onclick=async()=>{
  started=true;error.textContent="";
  if(activeId!==null){cover.classList.add("hidden");try{await video.play();}catch(e){fail(e);}return;}
  await next();
};
setInterval(async()=>{
  if(!started||inFlight||activeId!==null)return;
  await next();
},2000);
setInterval(checkPreload,8000);
