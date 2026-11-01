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
const banner=document.getElementById("song-banner");
const bannerTransparency=document.getElementById("banner-transparency");
const bannerTransparencyValue=document.getElementById("banner-transparency-value");
const smoothBuffer=document.getElementById("smooth-buffer");
const nowPlaying=document.getElementById("now-playing");
const progress=document.getElementById("progress");
const nextList=document.getElementById("next-list");
const nextUp=document.getElementById("next-up");
const nextUpRow=document.getElementById("next-up-row");
const controlError=document.getElementById("control-error");
const captions=document.getElementById("html-captions");
const offsetControls=document.getElementById("offset-controls");
const offsetValue=document.getElementById("offset-value");
let offsetTenths=0;
let activeOverlay=false;
let activeBakedUrl=null;
function overlaySource(song){return song.html_overlay?.source_url||null;}
function mediaSource(song){return overlaySource(song)||song.media_url;}
function resetOverlay(){
  TVOverlay.reset();activeOverlay=false;offsetTenths=0;
  offsetValue.textContent="+0.0 秒";offsetControls.hidden=true;
}
for(const button of document.querySelectorAll("[data-offset]")){
  button.onclick=()=>{
    if(!activeOverlay)return;
    offsetTenths=Math.max(-600,Math.min(600,offsetTenths+Number(button.dataset.offset)));
    TVOverlay.setOffset(offsetTenths);
    offsetValue.textContent=(offsetTenths>=0?"+":"")+(offsetTenths/10).toFixed(1)+" 秒";
    TVOverlay.draw(video.currentTime,video);
    showPanel();
  };
}
async function loadOverlay(song){
  resetOverlay();
  if(!song.html_overlay)return false;
  try{
    const resp=await fetch(song.html_overlay.timeline_url,{cache:"no-store"});
    if(!resp.ok)throw Error("字幕数据不可用");
    TVOverlay.setup(await resp.json(),captions);
    activeOverlay=true;offsetControls.hidden=false;
    return true;
  }catch{
    resetOverlay();return false;
  }
}

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
let nextKey=null;
let highlightTimer=null;
let playbackMode="browser";
const START_BUFFER_SECONDS=12;
const START_BUFFER_WAIT_MS=12000;
let warmup=null;
let warmupTimer=null;
function setBannerTransparency(value){
  const transparency=Math.max(0,Math.min(100,Number(value)||0));
  banner.style.setProperty("--banner-opacity",String(1-transparency/100));
  bannerTransparency.value=String(transparency);
  bannerTransparencyValue.textContent=transparency+"%";
}
let savedTransparency=15;
try{
  const saved=localStorage.getItem("vcktv-banner-transparency");
  if(saved!==null&&saved.trim()!==""&&Number.isFinite(Number(saved)))savedTransparency=Number(saved);
}catch{}
setBannerTransparency(savedTransparency);
bannerTransparency.oninput=()=>{
  setBannerTransparency(bannerTransparency.value);
  try{localStorage.setItem("vcktv-banner-transparency",bannerTransparency.value);}catch{}
};
let smoothPlayback=false;
try{smoothPlayback=localStorage.getItem("vcktv-smooth-buffer")==="1";}catch{}
smoothBuffer.checked=smoothPlayback;
smoothBuffer.onchange=()=>{
  smoothPlayback=smoothBuffer.checked;
  try{localStorage.setItem("vcktv-smooth-buffer",smoothPlayback?"1":"0");}catch{}
  if(!smoothPlayback&&warmup){
    cancelWarmup();
    video.play().catch(()=>{status.textContent="请手动点击继续播放";});
  }
};
function bufferedAhead(element){
  for(let i=0;i<element.buffered.length;i++){
    if(element.buffered.start(i)<=element.currentTime+0.05&&
       element.currentTime<=element.buffered.end(i)){
      return Math.max(0,element.buffered.end(i)-element.currentTime);
    }
  }
  return 0;
}
function cancelWarmup(){
  clearTimeout(warmupTimer);
  warmupTimer=null;
  warmup=null;
}
function playAfterBuffer(song){
  cancelWarmup();
  const element=video;
  const request={id:song.id,element};
  const startedAt=performance.now();
  warmup=request;
  const tick=async()=>{
    if(warmup!==request||activeId!==song.id||video!==element)return;
    const remaining=Number.isFinite(element.duration)?
      Math.max(0,element.duration-element.currentTime):START_BUFFER_SECONDS;
    const target=Math.min(START_BUFFER_SECONDS,remaining);
    const ahead=bufferedAhead(element);
    if(ahead>=target||performance.now()-startedAt>=START_BUFFER_WAIT_MS||element.error){
      cancelWarmup();
      status.textContent=songLabel(song);
      try{await element.play();}catch(e){
        if(activeId!==song.id||video!==element)return;
        status.textContent="请点击画面以允许播放声音";
        cover.classList.remove("hidden");start.textContent="▶ 恢复播放";
      }
    }else{
      status.textContent=songLabel(song)+"  缓冲中 "+Math.floor(ahead)+"/"+Math.ceil(target)+" 秒";
      warmupTimer=setTimeout(tick,250);
    }
  };
  tick();
}
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
function songLabel(song){return song.title+"  "+song.nickname;}
function updateNextSong(song){
  const key=song?JSON.stringify([song.id,song.title,song.nickname]):"";
  if(nextKey===key)return;
  nextUp.textContent=song?songLabel(song):"暂无下一首可播放歌曲";
  if(nextKey!==null){
    nextUpRow.classList.add("highlight");
    clearTimeout(highlightTimer);
    highlightTimer=setTimeout(()=>nextUpRow.classList.remove("highlight"),2200);
  }
  nextKey=key;
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
    updateNextSong(queue[0]);
    nextList.replaceChildren();
    if(!queue.length){
      const empty=document.createElement("li");empty.textContent="暂无下一首可播放歌曲";nextList.append(empty);
    }
    for(const [index,song] of queue.entries()){
      const li=document.createElement("li");
      li.textContent=(index+1)+". "+songLabel(song);
      nextList.append(li);
    }
    nowPlaying.textContent=state.current?songLabel(state.current):"暂无正在播放的歌曲";
    if(!started)status.textContent=state.current?nowPlaying.textContent:"等待播放";
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
function preloadKey(song){
  // Signed URLs change expiry between polls. Compare stable media identity instead.
  return JSON.stringify([song.id,song.media_file,song.source_file||"",Boolean(song.html_overlay)]);
}
async function checkPreload(){
  if(!started||activeId===null||inFlight||preloadInFlight||!Number.isFinite(video.duration))return;
  const remaining=video.duration-video.currentTime;
  if(remaining<=0)return;
  const ahead=bufferedAhead(video);
  // Aggressive preloading must not starve the current 4 Mbps stream.
  if(smoothPlayback&&preloaded&&ahead<Math.min(8,remaining))clearPreload();
  if(remaining>Math.min(smoothPlayback?120:60,video.duration/2))return;
  if(ahead<Math.min(smoothPlayback?20:10,remaining))return;
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
    standby.src=mediaSource(candidate);
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
    cancelWarmup();
    activeBakedUrl=answer.active.media_url;
    const useOverlay=await loadOverlay(answer.active);
    const sourceUrl=useOverlay?mediaSource(answer.active):answer.active.media_url;
    if(preloaded&&Boolean(preloaded.html_overlay)===useOverlay&&
       preloadKey(preloaded)===preloadKey(answer.active)&&!standby.error){
      const old=video;
      video=standby;
      standby=old;
      video.classList.remove("preloading");
      standby.classList.add("preloading");
      preloaded=null;
    }else{
      clearPreload();
      video.src=sourceUrl;
    }
    preloadFailed=null;
    lastProbe=-Infinity;
    status.textContent=songLabel(answer.active);
    cover.classList.add("hidden");
    updateControls();
    syncQueue();
    if(smoothPlayback)playAfterBuffer(answer.active);
    else{
      try{await video.play();}catch(e){
        status.textContent="请点击画面以允许播放声音";
        cover.classList.remove("hidden");start.textContent="▶ 恢复播放";
      }
    }
  }catch(e){fail(e);}finally{inFlight=false;}
}
async function complete(reason=""){
  if(activeId===null||finishing)return;
  finishing=true;
  cancelWarmup();
  resetOverlay();activeBakedUrl=null;
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
    if(activeId!==null){
      if(activeOverlay&&activeBakedUrl){
        const fallback=activeBakedUrl;const position=video.currentTime;
        resetOverlay();activeBakedUrl=null;video.src=fallback;
        video.addEventListener("loadedmetadata",()=>{video.currentTime=position;video.play().catch(fail);},{once:true});
        status.textContent="原片不可播放，已回退烧录版";
      }else{status.textContent="媒体无法播放，正在跳过这一首……";complete("skip");}
    }
  });
  element.addEventListener("timeupdate",()=>{if(element===video){checkPreload();updateControls();}});
  element.addEventListener("play",()=>{if(element===video)updateControls();});
  element.addEventListener("pause",()=>{if(element===video)updateControls();});
}
start.onclick=async()=>{
  started=true;error.textContent="";
  if(activeId!==null){
    cancelWarmup();cover.classList.add("hidden");
    try{await video.play();}catch(e){fail(e);}return;
  }
  await next();
};
setInterval(async()=>{
  if(!started||inFlight||activeId!==null)return;
  await next();
},2000);
setInterval(checkPreload,8000);

pauseResume.onclick=async()=>{
  if(activeId===null||playbackMode!=="browser")return;
  if(video.paused){cancelWarmup();try{await video.play();}catch(e){controlError.textContent=e.message;}}
  else video.pause();
  showPanel();updateControls();
};
restart.onclick=async()=>{
  if(activeId===null||finishing||playbackMode!=="browser")return;
  cancelWarmup();video.currentTime=0;
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

function overlayFrame(){if(activeOverlay)TVOverlay.draw(video.currentTime,video);requestAnimationFrame(overlayFrame);}
requestAnimationFrame(overlayFrame);
