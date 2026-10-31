"use strict";
let video=document.getElementById("video");
let standby=document.getElementById("preload");
const cover=document.getElementById("cover");
const status=document.getElementById("status");
const start=document.getElementById("start");
const error=document.getElementById("error");
const panel=document.getElementById("controls");
const toggle=document.getElementById("panel-toggle");
const pauseResume=document.getElementById("pause-resume");
const restart=document.getElementById("restart");
const skip=document.getElementById("skip");
const replayNext=document.getElementById("replay-next");
const volume=document.getElementById("volume");
const nowPlaying=document.getElementById("now-playing");
const progress=document.getElementById("progress");
const nextList=document.getElementById("next-list");
const nextTip=document.getElementById("next-tip");
const controlError=document.getElementById("control-error");
const token=new URLSearchParams(location.search).get("token");
let activeId=null;
let started=false;
let inFlight=false;
let finishing=false;
let preloaded=null;
let preloadFailed=null;
let preloadInFlight=false;
let lastProbe=-Infinity;
let replayInFlight=false;
let panelTimer=null;
let tipTimer=null;
let nextKey=null;
let lastEndTipKey=null;
let playbackMode="browser";
function showPanel(){
  panel.hidden=false;toggle.setAttribute("aria-expanded","true");
  clearTimeout(panelTimer);
  panelTimer=setTimeout(()=>{
    if(!panel.matches(":hover")&&!panel.matches(":focus-within"))hidePanel();
  },6500);
}
function hidePanel(){panel.hidden=true;toggle.setAttribute("aria-expanded","false");}
toggle.onclick=()=>panel.hidden?showPanel():hidePanel();
panel.onpointermove=showPanel;
panel.onpointerleave=()=>{
  clearTimeout(panelTimer);
  panelTimer=setTimeout(()=>{if(!panel.matches(":focus-within"))hidePanel();},4000);
};
function hint(message){
  nextTip.textContent=message;nextTip.hidden=false;
  clearTimeout(tipTimer);tipTimer=setTimeout(()=>{nextTip.hidden=true;},4200);
}
function clock(seconds){
  if(!Number.isFinite(seconds))return "0:00";
  return Math.floor(seconds/60)+":"+String(Math.floor(seconds%60)).padStart(2,"0");
}
function updateControls(){
  const available=activeId!==null&&playbackMode==="browser"&&!finishing;
  for(const button of [pauseResume,restart,skip,replayNext])button.disabled=!available;
  replayNext.disabled=!available||replayInFlight;
  pauseResume.textContent=video.paused?"▶ 继续":"⏸ 暂停";
  progress.textContent=clock(video.currentTime)+" / "+clock(video.duration);
}
async function syncQueue(){
  try{
    const state=await api("/api/tv/state");
    playbackMode=state.playback_mode||"browser";
    const queue=(state.queue||[]).slice(0,3);
    const key=JSON.stringify(queue.map(s=>[s.id,s.title,s.nickname]));
    if(nextKey!==null&&key!==nextKey&&queue.length){
      hint("下一首更新："+queue[0].title+" / "+queue[0].nickname);
    }
    nextKey=key;
    const remaining=video.duration-video.currentTime;
    if(activeId!==null&&queue.length&&remaining>0&&remaining<=20){
      const endKey=activeId+":"+queue[0].id;
      if(lastEndTipKey!==endKey){
        lastEndTipKey=endKey;
        hint("即将播放："+queue[0].title+" / "+queue[0].nickname);
      }
    }
    nextList.replaceChildren();
    if(!queue.length){
      const empty=document.createElement("li");empty.textContent="暂无下一首可播放歌曲";nextList.append(empty);
    }
    for(const [index,song] of queue.entries()){
      const li=document.createElement("li");
      li.textContent=(index+1)+". "+song.title+" / "+song.nickname;
      nextList.append(li);
    }
    nowPlaying.textContent=state.current?state.current.title+" / "+state.current.nickname:"暂无正在播放的歌曲";
    if(playbackMode==="manual")controlError.textContent="当前为手动投屏模式，请到管理后台控制";
    else if(controlError.textContent.includes("手动投屏"))controlError.textContent="";
    if(preloaded&&(!queue.length||preloaded.id!==queue[0].id))clearPreload();
    updateControls();
  }catch(e){controlError.textContent=e.message||String(e);}
}
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
    lastEndTipKey=null;
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
    updateControls();
    syncQueue();
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
  syncQueue();
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
  element.addEventListener("timeupdate",()=>{if(element===video){checkPreload();updateControls();}});
  element.addEventListener("play",()=>{if(element===video)updateControls();});
  element.addEventListener("pause",()=>{if(element===video)updateControls();});
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

pauseResume.onclick=async()=>{
  if(activeId===null||playbackMode!=="browser")return;
  if(video.paused){try{await video.play();}catch(e){controlError.textContent=e.message;}}
  else video.pause();
  showPanel();updateControls();
};
restart.onclick=async()=>{
  if(activeId===null||finishing||playbackMode!=="browser")return;
  video.currentTime=0;
  try{await video.play();}catch(e){controlError.textContent=e.message;}
  showPanel();updateControls();
};
skip.onclick=()=>{
  if(activeId===null||finishing||playbackMode!=="browser")return;
  complete("skip");showPanel();
};
replayNext.onclick=async()=>{
  if(activeId===null||finishing||replayInFlight||playbackMode!=="browser")return;
  replayInFlight=true;updateControls();
  try{
    await api("/api/tv/replay-next","POST",{song_id:activeId});
    controlError.textContent="已安排当前歌曲下一首重唱";
    await syncQueue();
  }catch(e){controlError.textContent=e.message||String(e);}
  finally{replayInFlight=false;updateControls();}
  showPanel();
};
volume.oninput=()=>{video.volume=standby.volume=Number(volume.value)/100;showPanel();};
setInterval(syncQueue,3500);
syncQueue();updateControls();
