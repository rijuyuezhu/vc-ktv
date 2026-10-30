"use strict";
const $ = (id) => document.getElementById(id);
let preview = null;
let admin = false;
let currentMediaId = null;
let manualMode = false;
let popupSongId = null;
let toastTimer = null;
let refreshing = false;
let refreshQueued = false;
let refreshFailed = false;
let guestStateKey = null;
let adminStateKey = null;
const listKeys = new Map();
let selectingSongId = null;
let selectionRequest = 0;
let lyricControllers = [];
let lyricKeys = new Set();
let lyricHits = 0;
let lyricPending = 0;
let currentEventId = null;
let leadSeconds = 1.8;
let archiveListKey = null;
let settingsEventId = null;
let adminAccountsEventId = null;
let adminCurrentEvent = null;
let guestAuthenticated = false;
let detailKey = null;
let playedKey = null;
const player = $("player");


function toast(message, error=false) {
  clearTimeout(toastTimer);
  const t = $("toast"); t.textContent = message;
  t.className = "show" + (error ? " error" : "");
  toastTimer = setTimeout(() => { t.className = ""; }, 4800);
}
async function api(url, method="GET", body=null, signal=null) {
  let opts = {method, credentials:"same-origin", cache:"no-store", signal};
  if (body !== null) {
    if (body instanceof FormData) opts.body = body;
    else { opts.headers = {"Content-Type":"application/json"}; opts.body = JSON.stringify(body); }
  }
  const res = await fetch(url, opts);
  let data;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}
function error(e) {toast(e.message || "操作失败", true);}
async function guarded(action, button=null) {
  try {
    if (button) button.disabled = true;
    return await action();
  } catch (e) { error(e); }
  finally { if (button) button.disabled = false; }
}
function duration(n) {
  n = Math.round(Number(n) || 0);
  return `${Math.floor(n/60)}:${String(n%60).padStart(2,"0")}`;
}
function appendText(parent, tag, text, cls="") {
  const el = document.createElement(tag);
  el.textContent = text;
  if (cls) el.className = cls;
  parent.append(el);
  return el;
}
function preparationStatus(song) {
  const stages={
    queued:"等待字幕制作",extract:"提取音频",kim:"分离人声",
    align:"语音对齐",subtitles:"生成字幕",render:"烧录字幕"
  };
  const states={
    pending:"◷ 等待下载",downloading:"◷ 下载原片中",
    downloaded:song.lyric_selected?"◷ 等待字幕制作":"◷ 等待歌词",
    original:song.skip_subtitles?"✓ 使用原片":"! 字幕失败",
    unavailable:"! 下载失败",done:"✓ 视频已就绪",manual:"✓ 视频已就绪"
  };
  if(song.prep_state==="aligning")return "◷ "+(stages[song.prep_stage]||"制作字幕中");
  return states[song.prep_state]||"◷ 准备中";
}
function addDownload(parent,song) {
  if(!song.downloadable)return;
  const link=appendText(parent,"a",song.prep_state==="original"?"↓ 下载原片":"↓ 下载视频","mini download-link");
  link.href="/api/songs/"+song.id+"/download";
  link.onclick=(event)=>event.stopPropagation();
  link.title="保存到手机后，在乐播投屏的本地视频或相册中选择";
}
async function cancelGuestSong(songId){
  if(!confirm("取消自己点的这首歌？"))return;
  await api(`/api/songs/${songId}/cancel`,"POST");
  if(selectingSongId===songId && $("guest-lyrics-dialog").open)$("guest-lyrics-dialog").close();
  if(currentRoute()==="song-"+songId)window.location.hash="";
  toast("✓ 已取消点歌");
  await refresh();
}
function makeSong(song, order, mode="row") {
  const outer = document.createElement("div");outer.className="song-item";
  if(["row","status","played"].includes(mode)){
    outer.classList.add("song-open");outer.tabIndex=0;outer.setAttribute("role","link");
    outer.setAttribute("aria-label","详情："+song.title);
    outer.onclick=(e)=>{
      if(e.target.closest("button,a,input,label"))return;
      window.location.hash="song-"+song.id;
    };
    outer.onkeydown=(e)=>{
      // A focused download link retains its own native keyboard action.
      if(e.target!==outer)return;
      if(e.key==="Enter"||e.key===" "){e.preventDefault();outer.click();}
    };
  }
  appendText(outer,"span",String(order),"ordinal");
  const copy = document.createElement("div");copy.className="song-copy";outer.append(copy);
  appendText(copy,"strong",song.title+(song.page>1?` (P${song.page})`:"")+
    (song.priority_next?(song.replay_of?"  ↻ 下一首重唱":"  ↥ 下一首播放"):""));
  appendText(copy,"small",`${song.nickname}  ${duration(song.duration)}  ${song.uploader}`);
  if(mode==="row" && song.mine && ["waiting","ready","rendering"].includes(song.status)){
    const detail=appendText(outer,"button","详情 / 取消","mini");
    detail.onclick=(e)=>{e.stopPropagation();window.location.hash="song-"+song.id;};
  }
  if(mode==="guest" && song.mine && ["ready","waiting"].includes(song.status) &&
     song.prep_state!=="aligning"){
    const pick=appendText(outer,"button",song.lyric_selected?"♫ 换歌词":"♫ 选歌词","mini lyric-pick");
    pick.onclick=()=>guarded(()=>openLyricPicker(song.id,song.title,song.lyric_query),pick);
  }
  if(mode==="guest" && song.mine && ["waiting","ready","rendering"].includes(song.status)){
    const cancel=appendText(outer,"button","✕ 取消","mini");
    cancel.title="仅取消自己尚未开唱的歌曲";
    cancel.onclick=()=>guarded(()=>cancelGuestSong(song.id),cancel);
  }
  if(mode==="status"){
    const status=preparationStatus(song);
    const stateLabel=appendText(outer,"span",status,"status"+(status.startsWith("!")?" wait":""));
    if(song.prep_error)stateLabel.title=song.prep_error;
    if(song.prep_state==="unavailable"){
      const a=appendText(outer,"a","↗ B站");a.href="https://www.bilibili.com/video/"+song.bvid+"/?p="+song.page;a.target="_blank";a.rel="noopener noreferrer";
    }
  }else if(mode==="admin"){
    const stageNames={
      download:"下载原片",downloaded:"原片已下载",queued:"等待模型",
      extract:"提取音频",kim:"Kim 人声分离",
      align:"Qwen/TIFA 对齐",subtitles:"生成字幕",render:"FFmpeg 烧录",
      done:"字幕已完成",skipped:"使用原视频"
    };
    const stage=stageNames[song.prep_stage];
    if(stage && ["waiting","ready"].includes(song.status) &&
       ["downloading","aligning","original","downloaded"].includes(song.prep_state)){
      const elapsed=song.prep_stage_at?
        Math.max(0,Math.floor(Date.now()/1000-song.prep_stage_at)):0;
      const suffix=["downloading","aligning"].includes(song.prep_state)&&elapsed>=60?
        "（已进行 "+Math.floor(elapsed/60)+" 分钟）":"";
      const line=appendText(copy,"small",stage+suffix,"muted");
      line.title="按阶段变更更新，不通过 GPU 采样估算百分比";
    }
    const actions=document.createElement("div");actions.className="song-actions";outer.append(actions);
    if(song.status==="waiting" && !["downloading","pending"].includes(song.prep_state)){
      const upload=appendText(actions,"label","上传 MP4","mini upload-label");
      const file=document.createElement("input");file.type="file";file.accept=".mp4,.webm,.m4v,video/mp4,video/webm";
      upload.append(file);file.addEventListener("change",() => {
        if(!file.files[0])return;
        guarded(async()=>{
          const fd=new FormData();fd.append("file",file.files[0]);
          await api(`/api/admin/songs/${song.id}/media`,"POST",fd);
          toast("✓ 已上传"); await refresh();
        },upload);
      });
    }
    const lyrics=appendText(actions,"button","歌词","mini");lyrics.onclick=()=>editLyrics(song.id,song.title);
    if(song.status==="ready" && (song.prep_state==="original" || song.prep_state==="done")){
      const done=song.prep_state==="done";
      const retry=appendText(
        actions,"button",done?"↻ 按新风格重制":
        song.skip_subtitles?"↻ 重新生成字幕":"↻ 重试字幕","mini"
      );
      retry.title=done?"使用当前本场字幕预设重新制作，旧成片保留到新片完成":song.prep_error||"只重试字幕，保留原视频";
      retry.onclick=()=>guarded(async()=>{
        if(done&&!confirm("按当前字幕风格重新制作这首歌？重制完成前旧成片会保留。"))return;
        await api(`/api/admin/songs/${song.id}/subtitles/retry`,"POST");
        toast("◷ 已重新排队制作字幕");await refresh();
      },retry);
    }
    if(song.status==="ready" && song.has_media &&
       ["downloaded","original","aligning"].includes(song.prep_state) &&
       !song.skip_subtitles){
      const bypass=appendText(actions,"button","▶ 原片入队","mini");
      bypass.title="仅此首歌跳过字幕，立即以原视频排队；已经开始的推理不会立刻停止";
      bypass.onclick=()=>guarded(async()=>{
        if(!confirm("仅让这首歌以原视频进入队列？字幕制作结果将被忽略。"))return;
        await api(`/api/admin/songs/${song.id}/subtitles/skip`,"POST");
        toast("✓ 已允许原片入队");await refresh();
      },bypass);
    }
    if(song.status==="ready" && song.prep_state!=="aligning"){
      const render=appendText(actions,"button","手工制作 KTV","mini");render.onclick=()=>guarded(async()=>{
        await api(`/api/admin/songs/${song.id}/render`,"POST",{lead:leadSeconds});
        toast("◷ 正在制作字幕");await refresh();
      },render);
    }else if(song.status==="rendering"){
      appendText(actions,"span","制作中","status wait");
    }
    if(song.status==="ready" && song.playable){
      if(song.priority_next){
        appendText(actions,"span","✓ 已设为下一首","status");
      }else{
        const pin=appendText(actions,"button","↥ 下一首播放","mini");
        pin.title="当前歌曲播放完后优先播放本首，不会打断当前播放";
        pin.onclick=()=>guarded(async()=>{
          await api(`/api/admin/songs/${song.id}/pin-next`,"POST");
          toast("✓ 已安排为下一首");await refresh();
        },pin);
      }
    }
    if(["waiting","ready","rendering"].includes(song.status)){
      const cancel=appendText(actions,"button","✕ 取消点歌","mini");
      cancel.title="取消这首待播歌曲；若正在推理，当前计算可能仍需结束";cancel.onclick=()=>guarded(async()=>{
      if(!confirm("取消这首歌曲？"))return;
      await api(`/api/admin/songs/${song.id}/cancel`,"POST"); await refresh();
    },cancel);
    }
    if(song.status==="played"){
      const replay=appendText(actions,"button","下一首重唱","mini");
      replay.onclick=()=>guarded(async()=>{
        if(!confirm("把这首歌安排到下一首？"))return;
        await api(`/api/admin/songs/${song.id}/replay`,"POST");
        toast("已加入队列");detailKey=null;await refresh();
      },replay);
    }
  }else{
    const status=song.status==="ready"&&!song.awaiting_subtitles?
      "✓ 可播放":preparationStatus(song);
    appendText(outer,"span",status,"status"+(status.startsWith("!")?" wait":""));
  }
  addDownload(outer,song);
  return outer;
}
function fillList(id, songs, mode="row") {
  const key=JSON.stringify([mode,songs]);
  if(listKeys.get(id)===key)return;
  listKeys.set(id,key);
  const target=$(id);target.replaceChildren();
  if(!songs.length){ appendText(target,"div",mode==="admin"?"当前没有待处理的歌曲":"暂无歌曲","empty"); return; }
  songs.forEach((s,i)=>target.append(makeSong(s,i+1,mode)));
}
async function refreshSongHistory(){
  const data=await api("/api/songs/played");
  const key=JSON.stringify(data.songs);
  if(playedKey===key)return;
  playedKey=key;
  const target=$("page-played");target.replaceChildren();
  if(!data.songs.length){appendText(target,"p","还没有已唱歌曲","empty");return;}
  const list=appendText(target,"div","","song-list");
  data.songs.forEach((song,index)=>list.append(makeSong(song,index+1,"played")));
}
async function refreshSongDetail(){
  const id=Number(currentRoute().slice(5));
  if(!Number.isInteger(id)||id<=0)return;
  const song=await api(`/api/songs/${id}/detail`);
  const key=JSON.stringify([song,isAdminPath()]);
  if(detailKey===key){
    const running=$("page-song").querySelector(".stage-current-time");
    if(running&&song.prep_stage_at)running.textContent="进行中 "+
      Math.max(0,Math.floor(Date.now()/1000-song.prep_stage_at))+" 秒";
    return;
  }
  detailKey=key;
  $("subpage-back").href=song.status==="played"?"#played":"#";
  const target=$("page-song");
  const timingWasOpen=Boolean(target.querySelector(".timing-details")?.open);
  target.replaceChildren();
  const panel=appendText(target,"section","","panel song-detail-panel");
  appendText(panel,"h2",song.title);
  appendText(panel,"small",song.nickname+"  "+duration(song.duration),"muted");
  if(song.downloadable){
    const download=appendText(panel,"a","↓ 下载视频","btn primary detail-download");
    download.href=`/api/songs/${id}/download`;
  }
  appendText(panel,"small",song.status==="played"?"已唱完":preparationStatus(song),"muted");
  if(isAdminPath()){
    const names={download:"下载",extract:"提取音频",kim:"人声分离",
      align:"歌词对齐",subtitles:"字幕生成",render:"视频烧录"};
    const entries=Object.entries(names).filter(([key])=>song.stage_seconds?.[key]!=null);
    const running=["downloading","aligning"].includes(song.prep_state);
    if(entries.length||running){
      const details=appendText(panel,"details","");
      details.className="timing-details";
      details.open=timingWasOpen;
      appendText(details,"summary","处理耗时");
      for(const [key,label] of entries){
        const line=appendText(details,"div","","timing-row");
        appendText(line,"span",label);
        appendText(line,"span",Number(song.stage_seconds[key]).toFixed(1)+" 秒");
      }
      if(running && song.prep_stage_at){
        const line=appendText(details,"div","","timing-row");
        appendText(line,"span",names[song.prep_stage]||"制作中");
        const seconds=Math.max(0,Date.now()/1000-song.prep_stage_at);
        appendText(line,"span","进行中 "+seconds.toFixed(0)+" 秒","stage-current-time");
      }
      const total=entries.reduce((sum,[key])=>sum+Number(song.stage_seconds[key]),0);
      if(entries.length){
        const line=appendText(details,"div","","timing-row");
        appendText(line,"strong","合计");
        appendText(line,"strong",total.toFixed(1)+" 秒");
      }
    }
  }
  const row=makeSong(song,1,isAdminPath()?"admin":"guest");
  row.querySelector(".download-link")?.remove();
  row.querySelector(".song-copy")?.remove();
  row.querySelector(".ordinal")?.remove();
  if(row.querySelector(".song-actions,.lyric-pick,button"))panel.append(row);
  if(isAdminPath()&&song.status==="ready"){
    const extras=appendText(panel,"details","");
    appendText(extras,"summary","手工字幕参数");
    const label=appendText(extras,"label","提前秒数");
    label.htmlFor="detail-lead-seconds";
    const lead=document.createElement("input");
    lead.id="detail-lead-seconds";
    lead.type="number";lead.min="0";lead.max="5";lead.step="0.1";
    lead.value=String(leadSeconds);
    lead.oninput=()=>{leadSeconds=Number(lead.value);};
    extras.append(lead);
  }
  if(isAdminPath()&&song.prep_error){
    const details=appendText(panel,"details","");
    appendText(details,"summary","处理信息");
    appendText(details,"p",song.prep_error,"muted");
  }
}

async function fetchPreview(page=1) {
  const info=await api(`/api/preview?bvid=${encodeURIComponent($("bvid").value.trim())}&page=${page}`);
  preview=info;
  $("preview").classList.remove("hidden");
  $("preview-title").textContent=info.title;
  $("preview-owner").textContent=`UP 主：${info.uploader}`;
  $("preview-duration").textContent=`时长：${duration(info.duration)}`;
  $("preview-cover").src=info.cover;
  $("preview-cover").referrerPolicy="no-referrer";
  $("official-link").href=info.official_url;
  $("embed-container").classList.add("hidden");$("embed").removeAttribute("src");
  $("page-select").replaceChildren();
  info.pages.forEach(p=>{const opt=document.createElement("option");opt.value=p.page;opt.textContent=`P${p.page}  ${p.part}`;$("page-select").append(opt);});
  $("page-select").value=String(info.page);
  $("multi-page").classList.toggle("hidden",info.pages.length<2);
}
function abortLyricRequests(){
  lyricControllers.forEach(c=>c.abort());
  lyricControllers=[];
}
async function openLyricPicker(songId,title,query){
  selectingSongId=songId;
  $("guest-lyrics-song").textContent=title;
  // Server supplies the same extracted name used for the backend search.
  // Show it immediately so the guest can correct an incorrect video title.
  $("guest-lyrics-query").value=(query||title).slice(0,60);
  if(!$("guest-lyrics-dialog").open)$("guest-lyrics-dialog").showModal();
  await searchGuestLyrics();
}
function addLyricCandidates(candidates, songId, sequence){
  if(sequence!==selectionRequest || selectingSongId!==songId)return;
  const panel=$("guest-lyrics-results");
  candidates.forEach(candidate=>{
    if(lyricHits>=30)return;
    const key=candidate.title+"\u0000"+candidate.text;
    if(lyricKeys.has(key))return;
    lyricKeys.add(key);
    if(lyricHits===0)panel.replaceChildren();
    lyricHits++;
    const card=document.createElement("div");card.className="lyric-option";
    const heading=document.createElement("div");heading.className="lyric-option-header";
    appendText(heading,"strong",candidate.title);
    appendText(heading,"span",candidate.source);
    card.append(heading);
    if(candidate.artist)appendText(card,"div",candidate.artist,"muted");
    const lines=candidate.text.split("\n");
    appendText(card,"div",lines.slice(0,3).join(" / ").slice(0,155),"muted");
    const details=document.createElement("details");
    appendText(details,"summary","展开完整歌词");
    appendText(details,"pre",candidate.text);
    card.append(details);
    const button=appendText(card,"button","使用这一版歌词","btn primary");
    button.onclick=()=>guarded(async()=>{
      await api("/api/songs/"+songId+"/lyrics/select","POST",{
        text:candidate.text,ticket:candidate.ticket,source:candidate.source
      });
      selectionRequest++;
      abortLyricRequests();
      selectingSongId=null;
      $("guest-lyrics-dialog").close();
      toast("✓ 歌词已选择");
      await refresh();
    },button);
    panel.append(card);
  });
  updateLyricSearchStatus();
}
function updateLyricSearchStatus(){
  $("guest-lyrics-status").textContent=lyricHits?
    `已有 ${lyricHits} 版可选${lyricPending?"，其他来源仍在查询":""}`:
    (lyricPending?"正在查找歌词……":"没有找到候选，可以更换关键词或上传 LRC");
}
async function fetchLyricPhase(phase, songId, query, sequence){
  const controller=new AbortController();
  lyricControllers.push(controller);
  lyricPending++;
  const deadline=setTimeout(()=>controller.abort(),5000);
  try{
    const url="/api/songs/"+songId+"/lyrics/candidates?phase="+phase+"&query="+encodeURIComponent(query);
    const data=await api(url,"GET",null,controller.signal);
    if(sequence!==selectionRequest || selectingSongId!==songId)return;
    addLyricCandidates(data.candidates,songId,sequence);
  }catch(e){
    // A failed or slow provider contributes no cards. Other providers continue.
    if(e.name!=="AbortError")console.info("Lyrics provider",phase,e.message);
  }finally{
    clearTimeout(deadline);
    lyricControllers=lyricControllers.filter(c=>c!==controller);
    if(sequence===selectionRequest && selectingSongId===songId){
      lyricPending--;
      if(!lyricHits && !lyricPending){
        $("guest-lyrics-results").replaceChildren();
        appendText($("guest-lyrics-results"),"div","暂无歌词候选，可修改关键词或上传 LRC","empty");
      }
      updateLyricSearchStatus();
    }
  }
}
async function searchGuestLyrics(){
  const songId=selectingSongId;
  if(songId===null)return;
  const sequence=++selectionRequest;
  abortLyricRequests();
  lyricKeys=new Set();
  lyricHits=0;
  lyricPending=0;
  const query=$("guest-lyrics-query").value.trim();
  const panel=$("guest-lyrics-results");
  panel.replaceChildren();
  if(!query){appendText(panel,"div","请先输入歌名","empty");return;}
  appendText(panel,"div","正在查询歌词来源……","empty");
  $("guest-lyrics-more").disabled=false;
  const sources=["vcpedia","wiki","netease","lrclib"];
  // Each request adds results immediately on arrival, regardless of the others.
  await Promise.allSettled(sources.map(src=>fetchLyricPhase(src,songId,query,sequence)));
}
async function searchMoreLyrics(){
  const songId=selectingSongId;
  if(songId===null)return;
  const sequence=selectionRequest;
  const query=$("guest-lyrics-query").value.trim();
  if(!query)return;
  const button=$("guest-lyrics-more");
  button.disabled=true;
  await fetchLyricPhase("more",songId,query,sequence);
  if(sequence===selectionRequest && selectingSongId===songId)button.disabled=false;
}
$("guest-lyrics-dialog").addEventListener("close",()=>{
  selectingSongId=null;selectionRequest++;abortLyricRequests();
});
$("guest-lyrics-cancel").onclick=()=>guarded(()=>cancelGuestSong(selectingSongId),$("guest-lyrics-cancel"));
$("guest-lyrics-upload").onchange=()=>guarded(async()=>{
  const input=$("guest-lyrics-upload"), file=input.files[0], songId=selectingSongId;
  if(!file||songId===null)return;
  try{
    if(!file.name.toLowerCase().endsWith(".lrc")||file.size>128*1024)
      throw new Error("请选择不超过 128 KiB 的 .lrc 文件");
    const data=new FormData();data.append("file",file);
    await api(`/api/songs/${songId}/lyrics/upload`,"POST",data);
    $("guest-lyrics-dialog").close();
    toast("✓ 已上传歌词，将重新对齐时间轴");
    await refresh();
  }finally{input.value="";}
});
$("guest-lyrics-search").onclick=()=>guarded(()=>searchGuestLyrics(),$("guest-lyrics-search"));
$("guest-lyrics-more").onclick=()=>guarded(()=>searchMoreLyrics(),$("guest-lyrics-more"));
$("guest-lyrics-query").addEventListener("keydown",(e)=>{if(e.key==="Enter"){$("guest-lyrics-search").click();e.preventDefault();}});
$("quick-submit-btn").onclick=()=>guarded(async()=>{
  const bvid=$("bvid").value.trim();
  if(!bvid)throw new Error("请输入 BV 号");
  const result=await api("/api/songs","POST",{bvid,page:1});
  $("bvid").value="";
  $("preview").classList.add("hidden");
  preview=null;
  toast("已点歌，请选歌词");
  await refresh();
  await openLyricPicker(result.id,result.title,result.lyric_query);
},$("quick-submit-btn"));
$("preview-btn").onclick=()=>guarded(()=>fetchPreview(),$("preview-btn"));
$("bvid").addEventListener("keydown",(e)=>{if(e.key==="Enter")$("quick-submit-btn").click();});
$("page-select").onchange=()=>guarded(()=>fetchPreview(Number($("page-select").value)));
$("watch-btn").onclick=()=>{
  if(!preview)return;
  $("embed").src=preview.embed_url;
  $("embed-container").classList.remove("hidden");
};
$("submit-btn").onclick=()=>guarded(async()=>{
  if(!preview)throw new Error("先查询视频");
  const title=preview.title;
  const result=await api("/api/songs","POST",{bvid:preview.bvid,page:preview.page});
  toast("已点歌，请选歌词");$("preview").classList.add("hidden");
  preview=null;await refresh();
  await openLyricPicker(result.id,title,result.lyric_query);
},$("submit-btn"));
const isAdminPath=()=>window.location.pathname.replace(/\/$/, "")==="/admin";
function currentRoute(){return window.location.hash.replace(/^#\/?/, "");}
const adminRoutes={
  settings:["设置",null],
  "settings-playback":["播放与视频","event-settings-panel"],
  "settings-event":["场次管理","event-management"],
  "settings-accounts":["管理员账号","admin-accounts-panel"],
  "settings-history":["历史歌单","event-archive-panel"],
  stats:["演唱统计","performance-panel"]
};
function setupSubpages(){
  const root=$("subpage-content");
  const menu=document.createElement("nav");menu.className="panel page-links";
  for(const [route,label] of [
    ["settings-playback","播放与视频"],["settings-event","场次管理"],
    ["settings-accounts","管理员账号"],["settings-history","历史歌单"]
  ]){
    const link=appendText(menu,"a",label);
    link.href="#"+route;
  }
  const settings=document.createElement("div");settings.id="page-settings";
  settings.append(menu);root.append(settings);
  for(const name of ["played","song"]){
    const wrapper=document.createElement("div");wrapper.id="page-"+name;
    wrapper.className="hidden";root.append(wrapper);
  }
  for(const [route,[,nodeId]] of Object.entries(adminRoutes)){
    if(!nodeId)continue;
    const wrapper=document.createElement("div");wrapper.id="page-"+route;
    wrapper.className="hidden";
    const node=$(nodeId);
    if(node.tagName==="DETAILS"){node.open=true;node.classList.remove("fold-panel");}
    wrapper.append(node);root.append(wrapper);
  }
}
function showPage(){
  const isAdmin=isAdminPath();
  const route=currentRoute();
  const page=isAdmin&&adminRoutes[route]?route:
    route==="played"?"played":/^song-[0-9]+$/.test(route)?"song":"";
  const authenticated=isAdmin?admin:guestAuthenticated;
  $("guest-view").classList.toggle("hidden",isAdmin||Boolean(page&&authenticated));
  $("admin-view").classList.toggle("hidden",!isAdmin);
  $("admin-panel").classList.toggle("hidden",!admin||!!page);
  $("subpage").classList.toggle("hidden",!page||!authenticated);
  $("tab-guest").classList.toggle("selected",!isAdmin);
  $("tab-admin").classList.toggle("selected",isAdmin);
  $("tab-guest").setAttribute("aria-current",isAdmin?"false":"page");
  $("tab-admin").setAttribute("aria-current",isAdmin?"page":"false");
  $("hero-title").textContent=isAdmin?"管理 KTV":"想唱什么？";
  $("hero-desc").textContent=isAdmin?"当前队列":"选择歌曲，加入队列";
  $("hero-title").parentElement.classList.toggle("hidden",!!page);
  $("subpage-title").textContent=page==="played"?"已唱歌曲":page==="song"?"歌曲详情":
    page?adminRoutes[page][0]:"";
  $("subpage-back").href=page==="song"?(window.history.state?.fromPlayed?"#played":"#"):
    (page==="settings"||page==="stats"||page==="played")?"#":"#settings";
  $("page-settings").classList.toggle("hidden",page!=="settings");
  for(const name of ["played","song"])$("page-"+name).classList.toggle("hidden",page!==name);
  for(const routeName of Object.keys(adminRoutes)){
    if(routeName!=="settings")$("page-"+routeName).classList.toggle("hidden",page!==routeName);
  }
  document.title="VC KTV | "+(page==="played"?"已唱歌曲":page==="song"?"歌曲详情":
    page?adminRoutes[page][0]:isAdmin?"管理":"点歌");
}
setupSubpages();
window.addEventListener("hashchange",()=>{
  detailKey=null;playedKey=null;showPage();refresh();
});
$("show-user-login").onclick=()=>{
  $("auth-register").classList.add("hidden");$("auth-login").classList.remove("hidden");
};
$("show-user-register").onclick=()=>{
  $("auth-login").classList.add("hidden");$("auth-register").classList.remove("hidden");
};
$("login-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/login","POST",{
    username:$("admin-username").value.trim()||null,
    password:$("password").value
  });
  $("password").value="";admin=true;adminStateKey=null;toast("管理员登录成功");await refresh();
},$("login-btn"));
for(const id of ["password","admin-username"]){
  $(id).onkeydown=(e)=>{if(e.key==="Enter")$("login-btn").click();};
}
$("logout-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/logout","POST");admin=false;adminStateKey=null;currentMediaId=null;
  player.pause();player.removeAttribute("src");player.load();await refresh();
});
$("tv-link-btn").onclick=()=>{
  const tab=window.open("about:blank","_blank");
  guarded(async()=>{
    const link=await api("/api/admin/tv-link");
    if(tab)tab.location.href=link.url;
    else window.location.href=link.url;
  });
};
$("start-btn").onclick=()=>guarded(async()=>{await playNext();},$("start-btn"));
$("finish-btn").onclick=()=>guarded(async()=>{
  const wasManual=manualMode;
  const finished=await finishActive("completed",false);
  if(wasManual&&finished)await playNext();
},$("finish-btn"));
$("skip-btn").onclick=()=>guarded(async()=>{
  const wasManual=manualMode;
  const finished=await finishActive("skip",false);
  if(wasManual&&finished)await playNext();
},$("skip-btn"));

function updatePlaybackUI(){
  $("playback-mode").value=manualMode?"manual":"browser";
  $("browser-player").classList.toggle("hidden",manualMode);
  $("tv-link-btn").classList.toggle("hidden",manualMode);
  $("autoplay-label").classList.toggle("hidden",manualMode);
  $("manual-controls").classList.toggle("hidden",!manualMode);
  $("start-btn").textContent=manualMode?"▶ 领取下一首":"▶ 开始播放";
  $("finish-btn").textContent=manualMode?"✓ 唱完并切下一首":"✓ 唱完";
  $("skip-btn").textContent=manualMode?"跳过并切下一首":"跳过";
}
$("playback-mode").onchange=()=>guarded(async()=>{
  const mode=$("playback-mode").value;
  try{
    await api("/api/admin/playback-mode","PUT",{mode});
    manualMode=mode==="manual";
    currentMediaId=null;
    player.pause();player.removeAttribute("src");player.load();
    adminStateKey=null;updatePlaybackUI();await refresh();
  }catch(e){
    updatePlaybackUI();throw e;
  }
},$("playback-mode"));

async function playNext(){
  const data=await api("/api/admin/next","POST");
  if(!data.active){ toast("当前没有已准备好的歌曲");await refresh();return; }
  await assignPlayer(data.active,true);
  if(manualMode)toast("已领取当前歌曲，请下载后手动投屏");
  await refresh();
}
async function assignPlayer(active, start){
  if(!active || !active.media_url)return;
  if(manualMode){
    currentMediaId=active.id;
    $("playing-title").textContent="当前歌曲："+active.title+"  "+active.nickname;
    updatePlaybackUI();
    return;
  }
  if(currentMediaId!==active.id){
    currentMediaId=active.id;
    player.src=active.media_url;
    player.load();
    $("player-empty").classList.add("hidden");
  }
  $("playing-title").textContent=`正在播放：${active.title}  ${active.nickname}`;
  if(start){
    try{await player.play();}catch{toast("浏览器阻止自动播放，请手动点击播放器的播放按钮",true);}
  }
}
async function finishActive(reason, auto){
  const songId=currentMediaId;
  if(!songId) {toast("没有正在播放的歌曲");return false;}
  if(reason!=="completed")player.pause();
  await api("/api/admin/finish","POST",{song_id:songId,reason:reason==="skip"?"skip":""});
  currentMediaId=null;
  player.removeAttribute("src");player.load();
  $("player-empty").classList.remove("hidden");
  $("playing-title").textContent="当前没有正在播放的歌曲";
  updatePlaybackUI();
  if(auto && $("autoplay").checked && !manualMode)await playNext();
  else await refresh();
  return true;
}
player.addEventListener("ended",()=>{if(!manualMode)guarded(()=>finishActive("completed",true));});
player.addEventListener("error",()=>{if(player.currentSrc)toast("媒体播放失败，请检查编码或网络连接",true);});
async function editLyrics(songId, songTitle){
  popupSongId=songId;
  await guarded(async()=>{
    const data=await api(`/api/admin/songs/${songId}/lyrics`);
    $("lyrics-textarea").value=data.text;
    $("lyric-source-title").value=(songTitle||"").replace(/^【[^】]*】/,"").replace(/^\[[^\]]*\]/,"").slice(0,70);
    $("lyric-source-results").replaceChildren();
    $("lyrics-dialog").showModal();
  });
}
$("lyric-source-btn").onclick=()=>guarded(async()=>{
  const title=$("lyric-source-title").value.trim();
  if(!title)throw new Error("请输入 VCPedia 条目名称");
  const data=await api(`/api/admin/lyrics/vcpedia?title=${encodeURIComponent(title)}`);
  const results=$("lyric-source-results");results.replaceChildren();
  if(!data.candidates.length)appendText(results,"div","未找到歌词区块，请手动导入","empty");
  data.candidates.forEach((c,i)=>{
    const btn=appendText(results,"button",`使用候选 ${i+1}  ${c.text.split("\n").length} 行  需确认版本`,"mini");
    btn.onclick=()=>{if($("lyrics-textarea").value.trim()&&!confirm("替换现有编辑框内容？"))return;
      $("lyrics-textarea").value=c.text;toast("已填入候选，请核对并保存。纯文本不能直接制作 KTV 字幕");};
  });
},$("lyric-source-btn"));
$("textgrid-file").onchange=()=>guarded(async()=>{
  if(!popupSongId||!$("textgrid-file").files[0])return;
  const upload=new FormData();
  upload.append("file",$("textgrid-file").files[0]);
  const offset=Number($("alignment-offset").value);
  const result=await api("/api/admin/songs/"+popupSongId+"/alignment/import?offset="+encodeURIComponent(offset),"POST",upload);
  $("lyrics-textarea").value=result.lyrics;
  $("textgrid-file").value="";
  toast(`已导入 ${result.report.semantic_units} 个语义词的时间戳；请复核后再生成视频`);
  await refresh();
});
$("lyrics-save").onclick=()=>guarded(async()=>{
  if(popupSongId===null)return;
  await api(`/api/admin/songs/${popupSongId}/lyrics`,"POST",{text:$("lyrics-textarea").value});
  $("lyrics-dialog").close();toast("已保存参考歌词");await refresh();
},$("lyrics-save"));
async function refreshAdminAccounts(){
  const target=$("admin-account-list");
  const data=await api("/api/admin/accounts");
  target.replaceChildren();
  if(!data.accounts.length){appendText(target,"p","本场尚未创建管理员","muted");return;}
  data.accounts.forEach(account=>{
    const item=document.createElement("div");item.className="archive-item";
    appendText(item,"strong",account.username);
    const password=document.createElement("input");
    password.type="password";password.autocomplete="new-password";
    password.minLength=6;password.maxLength=72;password.placeholder="新密码（至少 6 个字符）";
    password.setAttribute("aria-label","重置 "+account.username+" 的密码");
    item.append(password);
    const reset=appendText(item,"button","重置密码","btn neutral");
    reset.type="button";
    reset.onclick=()=>guarded(async()=>{
      if(password.value.length<6)throw new Error("密码至少 6 个字符");
      await api("/api/admin/accounts/"+account.id+"/password","PUT",{password:password.value});
      password.value="";toast("密码已重置，旧登录及电视链接已失效");
    },reset);
    const revoke=appendText(item,"button","撤销","btn subtle");
    revoke.type="button";
    revoke.onclick=()=>guarded(async()=>{
      if(!confirm("撤销本场管理员「"+account.username+"」？当前登录及电视链接会立即失效。"))return;
      await api("/api/admin/accounts/"+account.id,"DELETE");
      toast("本场管理员已撤销");await refreshAdminAccounts();
    },revoke);
    target.append(item);
  });
}
$("admin-accounts-panel").addEventListener("toggle",()=>{
  if($("admin-accounts-panel").open && !$("admin-accounts-panel").classList.contains("hidden"))
    guarded(refreshAdminAccounts);
});
$("admin-account-create").onclick=()=>guarded(async()=>{
  const username=$("admin-account-name").value.trim();
  const password=$("admin-account-password").value;
  if(!username||password.length<6)throw new Error("请输入用户名和至少 6 位的密码");
  await api("/api/admin/accounts","POST",{username,password});
  $("admin-account-name").value="";
  $("admin-account-password").value="";
  toast("本场管理员已创建");await refreshAdminAccounts();
},$("admin-account-create"));
async function refreshEvents(){
  const data=await api("/api/admin/events");
  if(adminCurrentEvent!==data.active){
    adminCurrentEvent=data.active;
    detailKey=null;playedKey=null;
  }
  const active=data.events.find(e=>e.id===data.active);
  $("event-status").textContent=active?"♫ "+active.name:"尚未创建场次";
  $("event-join-code").textContent=active&&!active.legacy?"加入码  "+active.code:"";
  $("event-create-fields").classList.toggle("hidden",!!active||data.role!=="global");
  $("event-close-btn").classList.toggle("hidden",!active);
  $("event-settings-panel").classList.toggle("hidden",!active);
  const canManageAccounts=Boolean(active&&data.role==="global");
  document.querySelector('a[href="#settings-accounts"]')?.classList.toggle("hidden",data.role!=="global");
  if(data.role!=="global"&&currentRoute()==="settings-accounts")location.hash="settings";
  $("admin-accounts-panel").classList.toggle("hidden",!canManageAccounts);
  if(adminAccountsEventId!==(canManageAccounts?active.id:null)){
    adminAccountsEventId=canManageAccounts?active.id:null;
    $("admin-account-list").replaceChildren();
    if(canManageAccounts&&$("admin-accounts-panel").open)await refreshAdminAccounts();
  }
  $("admin-role").textContent=data.role==="global"?"全局管理员":
    "本场管理员："+(data.username||"");

  const key=JSON.stringify(data.events.map(e=>[e.id,e.name,e.status]));
  if(key!==archiveListKey){
    archiveListKey=key;
    const target=$("event-history");target.replaceChildren();
    if(!data.events.length)appendText(target,"p","暂无历史场次","muted");
    data.events.forEach(event=>{
      const item=document.createElement("div");item.className="archive-item";
      appendText(item,"span",event.name+"  "+(event.id===data.active?"进行中":"已归档"));
      const link=appendText(item,"a","↓ 导出 CSV","btn neutral");
      link.href="/api/admin/events/"+encodeURIComponent(event.id)+"/songs.csv";
      item.append(link);target.append(item);
    });
  }
  if(!active){settingsEventId=null;return;}
  const settings=await api("/api/admin/settings");
  if(settingsEventId!==active.id){
    settingsEventId=active.id;
    $("queue-policy").value=settings.queue_policy;
    $("subtitle-style").value=settings.subtitle_style;
    $("video-quality").value=String(settings.video_quality);
    $("allow-unsubtitled").checked=settings.allow_unsubtitled;
    $("compact-video").checked=settings.compact_video;
    $("pause-orders").checked=settings.orders_paused;
  }else if(document.activeElement!==$("queue-policy")&&
           document.activeElement!==$("allow-unsubtitled")&&
           document.activeElement!==$("subtitle-style")&&
           document.activeElement!==$("video-quality")&&
           document.activeElement!==$("compact-video")&&
           document.activeElement!==$("pause-orders")&&
           document.activeElement!==$("settings-save-btn")){
    $("queue-policy").value=settings.queue_policy;
    $("subtitle-style").value=settings.subtitle_style;
    $("video-quality").value=String(settings.video_quality);
    $("allow-unsubtitled").checked=settings.allow_unsubtitled;
    $("compact-video").checked=settings.compact_video;
    $("pause-orders").checked=settings.orders_paused;
  }
  updateStylePreview();
}
function updateStylePreview(){
  $("style-preview").className="style-preview "+$("subtitle-style").value;
}
$("subtitle-style").onchange=updateStylePreview;
$("settings-save-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/settings","PUT",{
    queue_policy:$("queue-policy").value,
    allow_unsubtitled:$("allow-unsubtitled").checked,
    subtitle_style:$("subtitle-style").value,
    video_quality:Number($("video-quality").value),
    compact_video:$("compact-video").checked,
    orders_paused:$("pause-orders").checked
  });
  adminStateKey=null;
  listKeys.clear();
  toast("✓ 已保存");
  await refresh();
},$("settings-save-btn"));
$("event-create-btn").onclick=()=>guarded(async()=>{
  const name=$("event-name").value.trim();
  const code=$("event-code").value.trim();
  if(!name)throw new Error("请填写场次名称");
  await api("/api/admin/events","POST",{name,code:code||null});
  $("event-name").value="";$("event-code").value="";
  toast("新场次已创建");
  await refresh();
},$("event-create-btn"));
$("event-close-btn").onclick=()=>guarded(async()=>{
  if(!confirm("结束本场 KTV 并保留全部记录？正在播放或制作时不能结束。"))return;
  await api("/api/admin/events/close","POST");
  toast("✓ 已归档");
  await refresh();
},$("event-close-btn"));

function renderEntry(entry){
  if(entry.event?.id!==currentEventId){
    detailKey=null;playedKey=null;
    currentEventId=entry.event?.id||null;
    $("join-code").value="";
    $("auth-login").classList.add("hidden");
    $("auth-register").classList.remove("hidden");
  }
  $("guest-entry-description").textContent=!entry.event
    ?"暂无活动场次"
    :entry.joined
      ?entry.event.name
      :entry.event.name+"  输入六位加入码";
  $("entry-code").classList.toggle("hidden",!entry.event||entry.joined);
  $("entry-account").classList.toggle("hidden",!entry.event||!entry.joined||entry.authenticated);
  guestAuthenticated=Boolean(entry.authenticated);
  $("guest-access").classList.toggle("hidden",entry.authenticated);
  $("guest-profile").classList.toggle("hidden",!entry.authenticated);
  $("request-panel").classList.toggle("hidden",!entry.authenticated);
  $("queue-panel").classList.toggle("hidden",!entry.authenticated);
  $("guest-greeting").textContent=entry.authenticated?"当前用户："+entry.username+"  "+entry.event.name:"";
  showPage();
}
$("join-btn").onclick=()=>guarded(async()=>{
  await api("/api/join","POST",{code:$("join-code").value.trim()});
  $("join-code").value="";
  await refresh();
},$("join-btn"));
$("register-btn").onclick=()=>guarded(async()=>{
  await api("/api/users/register","POST",{
    username:$("register-name").value.trim(),
    password:$("register-password").value,
    confirm_password:$("register-confirm").value
  });
  $("register-password").value="";$("register-confirm").value="";
  toast("✓ 已进入");
  await refresh();
},$("register-btn"));
$("user-login-btn").onclick=()=>guarded(async()=>{
  await api("/api/users/login","POST",{
    username:$("user-login-name").value.trim(),
    password:$("user-login-password").value
  });
  $("user-login-password").value="";
  toast("登录成功");await refresh();
},$("user-login-btn"));
$("user-logout-btn").onclick=()=>guarded(async()=>{
  await api("/api/users/logout","POST");
  await refresh();
},$("user-logout-btn"));

async function refresh(){
  if(refreshing){refreshQueued=true;return;}
  refreshing=true;
  try{
    const isGuest=!isAdminPath();
    if(isGuest){
      const entry=await api("/api/entry");
      renderEntry(entry);
      if(entry.authenticated){
        const state=await api("/api/state");
        const stateKey=JSON.stringify([entry.event?.id,state.current,state.queue,state.waiting]);
        if(currentRoute()==="played")await refreshSongHistory();
        if(/^song-[0-9]+$/.test(currentRoute()))await refreshSongDetail();
        if(guestStateKey!==stateKey){
          guestStateKey=stateKey;
          $("queue-count").textContent=state.queue.length+" 首可播";
          fillList("queue-ready",state.queue);
          fillList("queue-waiting",state.waiting);
          const current=$("current-guest");current.replaceChildren();
          appendText(current,"span",state.playback_mode==="manual"?
            "◷ 手动投屏中":"▶ 正在播放","current-label");
          if(state.current){
            appendText(current,"strong",state.current.title);
            appendText(current,"small","点歌者："+state.current.nickname);
            addDownload(current,state.current);
          }else appendText(current,"p","当前空闲，等待管理员开始播放");
        }
      }else{
        guestStateKey=null;
      }
    }
    if(isAdminPath()){
      let adm;
      try{adm=await api("/api/admin/state");admin=true;}
      catch(e){if(e.message.includes("管理员"))admin=false;else throw e;}
      $("login-box").classList.toggle("hidden",admin);
      $("admin-panel").classList.toggle("hidden",!admin||Boolean(adminRoutes[currentRoute()]||currentRoute()==="played"||/^song-[0-9]+$/.test(currentRoute())));
      $("subpage").classList.toggle("hidden",!admin||!(adminRoutes[currentRoute()]||currentRoute()==="played"||/^song-[0-9]+$/.test(currentRoute())));
      if(admin){
        await refreshEvents();
        const activeState=adm.active?{...adm.active,media_url:null}:null;
        manualMode=adm.playback_mode==="manual";
        $("playback-mode").disabled=Boolean(adm.active);
        const key=JSON.stringify([adm.waiting,adm.queue,adm.guests,activeState,adm.playback_mode]);
        updatePlaybackUI();
        if(key!==adminStateKey){
          adminStateKey=key;
          const pending=[...adm.queue,...adm.waiting];
          fillList("manage-status",pending,"status");
          // All song operations live in the full-page song detail.

          const stats=$("stats");stats.replaceChildren();
          if(!adm.guests.length)appendText(stats,"div","暂无演唱记录","empty");
          adm.guests.forEach(u=>{
            const c=document.createElement("div");c.className="stat";
            appendText(c,"strong",u.name);
            appendText(c,"small",u.plays+" 首  "+duration(u.seconds));stats.append(c);
          });
          $("current-song-download").classList.toggle("hidden",!adm.active);
          if(adm.active){
            $("current-song-download").href=`/api/songs/${adm.active.id}/download`;
            if(currentMediaId!==adm.active.id)await assignPlayer(adm.active,false);
            $("playing-title").textContent=(manualMode?"当前歌曲：":"正在播放：")+
              adm.active.title+"  "+adm.active.nickname;
          }else if(currentMediaId!==null){
            currentMediaId=null;player.pause();player.removeAttribute("src");player.load();
            $("player-empty").classList.remove("hidden");
            $("playing-title").textContent="当前没有正在播放的歌曲";
            updatePlaybackUI();
          }
        }
        if(currentRoute()==="played")await refreshSongHistory();
        if(/^song-[0-9]+$/.test(currentRoute()))await refreshSongDetail();
      }else {adminStateKey=null;$("subpage").classList.add("hidden");}
    }
    if(refreshFailed){toast("连接已恢复");refreshFailed=false;}
  }catch(e){
    if(!refreshFailed){error(e);refreshFailed=true;}
  }finally{
    refreshing=false;
    if(refreshQueued){refreshQueued=false;refresh();}
  }
}
showPage();
updatePlaybackUI();
refresh();
setInterval(()=>{if(document.visibilityState==="visible")refresh();},3500);
document.addEventListener("visibilitychange",()=>{
  if(document.visibilityState==="visible")refresh();
});
window.addEventListener("online",()=>refresh());
