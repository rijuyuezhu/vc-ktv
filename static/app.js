"use strict";
const $ = (id) => document.getElementById(id);
let preview = null;
let admin = false;
let currentMediaId = null;
let manualMode = false;
let playbackMode = "tv";
let tvRemoteState = null;
let currentAdminSong = null;
let nextAdminSong = null;
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
let gpuAlignment = true;
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
function songSource(song){
  return song.source_type==="netease"?
    {label:"🎵 网易云",copyLabel:"网易云链接",ref:song.source_id?
      "https://music.163.com/#/song?id="+encodeURIComponent(song.source_id):""}:
    {label:"📺 B站",copyLabel:"BV 号",ref:song.bvid||""};
}
function addSourceTag(parent,song){
  const isNetease=song.source_type==="netease";
  const tag=appendText(parent,"span","","source-tag");
  const image=appendText(tag,"img","");
  image.src=isNetease?"/icons/neteasecloudmusic.svg":"/icons/bilibili.svg";
  image.alt="";image.setAttribute("aria-hidden","true");
  appendText(tag,"span",isNetease?"网易云":"B站");
  tag.title=isNetease?"网易云音乐":"哔哩哔哩";
}
function sourceUrl(song){
  return song.source_type==="netease"?
    song.source_id?"https://music.163.com/#/song?id="+encodeURIComponent(song.source_id):"":
    song.bvid?"https://www.bilibili.com/video/"+encodeURIComponent(song.bvid)+"/?p="+(song.page||1):"";
}
async function copyValue(value,label){
  if(!value)return;
  try{
    if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(value);
    else{
      const field=document.createElement("textarea");
      field.value=value;field.style.cssText="position:fixed;left:-9999px";
      document.body.append(field);field.select();
      try{if(!document.execCommand("copy"))throw Error("clipboard");}finally{field.remove();}
    }
    toast("已复制"+label);
  }catch{prompt("复制"+label,value);}
}
function actionIcon(parent,kind){
  const icon=document.createElement("img");
  icon.src="/icons/"+kind+".svg";icon.alt="";
  icon.setAttribute("aria-hidden","true");
  icon.className="action-icon";
  parent.prepend(icon);
}
function detailMediaActions(panel,song){
  const section=appendText(panel,"section","","detail-section");
  appendText(section,"h3","视频与来源");
  const row=appendText(section,"div","","detail-tools");
  const link=(label,url,icon,external=false)=>{
    const a=appendText(row,"a",label,"btn neutral");
    actionIcon(a,icon);a.href=url;
    if(external){a.target="_blank";a.rel="noopener noreferrer";}
  };
  const copy=(label,value,what)=>{
    const btn=appendText(row,"button",label,"btn neutral");
    actionIcon(btn,"copy");
    btn.type="button";btn.onclick=()=>copyValue(value,what);
  };
  if(song.downloadable)link("下载成片","/api/songs/"+song.id+"/download","download");
  if(song.source_downloadable)link(song.source_type==="netease"?"下载原始视频":"下载原片",
    "/api/songs/"+song.id+"/download/source","download");
  if(song.bvid)copy("复制 BV",song.bvid,"BV 号");
  const url=sourceUrl(song);
  if(url){
    copy("复制链接",url,"链接");
    link("打开来源",url,"external-link",true);
  }
  if(!row.children.length)section.remove();
}
async function copySongSource(song){
  const source=songSource(song);
  if(!source.ref)return;
  try{
    if(navigator.clipboard?.writeText){
      await navigator.clipboard.writeText(source.ref);
    }else{
      // Local/tailnet HTTP may not provide the secure-context clipboard API.
      const field=document.createElement("textarea");
      field.value=source.ref;
      field.style.cssText="position:fixed;left:-9999px;top:0";
      document.body.append(field);field.focus();field.select();
      let copied=false;
      try{copied=document.execCommand("copy");}finally{field.remove();}
      if(!copied)throw new Error("clipboard unavailable");
    }
    toast("✓ 已复制"+source.copyLabel);
  }catch(e){
    prompt("复制"+source.copyLabel,source.ref);
  }
}
async function cancelGuestSong(songId){
  if(!confirm("取消自己点的这首歌？"))return;
  await api(`/api/songs/${songId}/cancel`,"POST");
  if(selectingSongId===songId && $("guest-lyrics-dialog").open)$("guest-lyrics-dialog").close();
  if(currentRoute()==="song-"+songId)window.location.hash="";
  toast("✓ 已取消点歌");
  await refresh();
}
function songState(song) {
  if(song.status==="playing")return "播放中";
  if(song.status==="played")return "已唱";
  if(song.status==="rendering")return "制作中";
  if(song.prep_state==="unavailable")return "下载失败";
  if(song.prep_state==="original"&&!song.skip_subtitles)return "字幕失败";
  if(song.status==="ready"&&!song.awaiting_subtitles)return "待播";
  if(song.prep_state==="downloaded"&&!song.lyric_selected&&!song.skip_subtitles)return "待选歌词";
  if(["aligning","downloading"].includes(song.prep_state))return "制作中";
  return "准备中";
}
function compactSong(song,order){
  const row=document.createElement("button");row.type="button";
  row.className="song-item song-row song-open";
  row.setAttribute("aria-label",song.title+"，点歌者 "+(song.nickname||"未知")+"，"+songState(song)+"，查看详情");
  appendText(row,"span",String(order),"ordinal");
  const copy=appendText(row,"span","","song-copy");
  appendText(copy,"strong",song.title+(song.source_type!=="netease"&&song.page>1?" (P"+song.page+")":""));
  const meta=appendText(copy,"span","","song-meta");
  appendText(meta,"span",song.nickname||"未知点歌者","song-requester");
  appendText(meta,"span","|","song-meta-separator").setAttribute("aria-hidden","true");
  appendText(meta,"span",song.uploader||"未知作者","song-author");
  addSourceTag(meta,song);
  const status=appendText(row,"span",songState(song),"song-state");
  if(["下载失败","字幕失败"].includes(status.textContent))status.classList.add("failed");
  appendText(row,"span","›","song-chevron").setAttribute("aria-hidden","true");
  row.onclick=()=>{window.location.hash="song-"+song.id;};
  return row;
}
function songAction(parent,label,run,style="neutral"){
  const button=appendText(parent,"button",label,"btn "+style);
  button.type="button";
  button.onclick=()=>guarded(()=>run(button),button);
  return button;
}
function detailActions(song,isAdmin){
  const actions=document.createElement("div");
  actions.className="detail-actions";
  const editable=["waiting","ready"].includes(song.status);
  if(!isAdmin){
    if(song.mine&&editable&&song.prep_state!=="aligning")
      songAction(actions,song.lyric_selected?"更换歌词":"选择歌词",
        ()=>openLyricPicker(song.id,song.title,song.lyric_query));
    if(song.mine&&["waiting","ready","rendering"].includes(song.status))
      songAction(actions,"取消点歌",()=>cancelGuestSong(song.id),"subtle");
    if(song.status==="played"&&song.downloadable)
      songAction(actions,"↻ 再来一首",async()=>{
        const next=await api("/api/songs/"+song.id+"/replay","POST");
        toast("已加入队列");window.location.hash="song-"+next.id;await refresh();
      },"primary");
  }else{
    if(editable&&song.prep_state!=="aligning")
      songAction(actions,"编辑歌词",()=>editLyrics(song.id,song.title));
    if(song.status==="waiting"&&song.prep_state==="unavailable")
      songAction(actions,"重试下载",async()=>{
        await api("/api/admin/songs/"+song.id+"/download/retry","POST");
        toast("已重试");await refresh();
      });
    if(song.status==="waiting"&&!["downloading","pending"].includes(song.prep_state)){
      const label=appendText(actions,"label","上传 MP4","btn neutral detail-upload");
      const file=document.createElement("input");file.type="file";
      file.accept=".mp4,.webm,.m4v,video/mp4,video/webm";
      label.append(file);
      file.onchange=()=>guarded(async()=>{
        if(!file.files[0])return;
        const data=new FormData();data.append("file",file.files[0]);
        await api("/api/admin/songs/"+song.id+"/media","POST",data);
        toast("已上传");await refresh();
      });
    }
    if(song.status==="ready"&&["original","done"].includes(song.prep_state))
      songAction(actions,song.prep_state==="done"?"按新风格重制":"重试字幕",async()=>{
        if(song.prep_state==="done"&&!confirm("按当前字幕样式重新制作？"))return;
        await api("/api/admin/songs/"+song.id+"/subtitles/retry","POST");
        toast("已开始制作");await refresh();
      });
    if(song.status==="ready"&&song.has_media&&
       ["downloaded","original","aligning"].includes(song.prep_state)&&!song.skip_subtitles)
      songAction(actions,"原片入队",async()=>{
        if(!confirm("跳过字幕，使用原片入队？"))return;
        await api("/api/admin/songs/"+song.id+"/subtitles/skip","POST");
        toast("已加入队列");await refresh();
      });
    if(song.status==="ready"&&song.prep_state!=="aligning")
      songAction(actions,"手工制作 KTV",async()=>{
        await api("/api/admin/songs/"+song.id+"/render","POST",{lead:leadSeconds});
        toast("制作中");await refresh();
      });
    if(song.status==="ready"&&song.playable&&!song.priority_next)
      songAction(actions,"下一首播放",async()=>{
        await api("/api/admin/songs/"+song.id+"/pin-next","POST");
        toast("已安排为下一首");await refresh();
      },"primary");
    if(song.status==="played"&&song.downloadable)
      songAction(actions,"↻ 下一首重唱",async()=>{
        if(!confirm("安排为下一首？"))return;
        await api("/api/admin/songs/"+song.id+"/replay","POST");
        toast("已安排为下一首");await refresh();
      },"primary");
    if(["waiting","ready","rendering"].includes(song.status))
      songAction(actions,"取消点歌",async()=>{
        if(!confirm("取消这首歌？"))return;
        await api("/api/admin/songs/"+song.id+"/cancel","POST");
        window.location.hash="";await refresh();
      },"subtle");
  }
  return actions.children.length?actions:null;
}
function fillList(id, songs, mode="row") {
  const key=JSON.stringify([mode,songs]);
  if(listKeys.get(id)===key)return;
  listKeys.set(id,key);
  const target=$(id);target.replaceChildren();
  if(!songs.length){ appendText(target,"div",mode==="admin"?"当前没有待处理的歌曲":"暂无歌曲","empty"); return; }
  songs.forEach((s,i)=>target.append(compactSong(s,i+1)));
}
async function refreshSongHistory(){
  const data=await api("/api/songs/played");
  const key=JSON.stringify(data.songs);
  if(playedKey===key)return;
  playedKey=key;
  const target=$("page-played");target.replaceChildren();
  if(!data.songs.length){appendText(target,"p","还没有已唱歌曲","empty");return;}
  const list=appendText(target,"div","","song-list");
  data.songs.forEach((song,index)=>list.append(compactSong(song,index+1)));
}
function songCoverUrl(cover){
  if(!cover)return "";
  try{
    const url=new URL(cover);
    if(url.protocol==="http:"&&(url.hostname==="hdslb.com"||url.hostname.endsWith(".hdslb.com")))
      url.protocol="https:";
    return url.href;
  }catch{return "";}
}
async function refreshSongDetail(){
  const id=Number(currentRoute().slice(5));
  if(!Number.isInteger(id)||id<=0)return;
  const song=await api("/api/songs/"+id+"/detail");
  const key=JSON.stringify([song,isAdminPath()]);
  if(detailKey===key){
    const running=$("page-song").querySelector(".stage-current-time");
    if(running&&song.prep_stage_at)running.textContent=
      Math.max(0,Math.floor(Date.now()/1000-song.prep_stage_at))+" 秒";
    return;
  }
  detailKey=key;
  const target=$("page-song");
  const timingWasOpen=Boolean(target.querySelector(".timing-details")?.open);
  target.replaceChildren();
  const panel=appendText(target,"section","","panel song-detail-panel");
  const header=appendText(panel,"div","","detail-header");
  const coverUrl=songCoverUrl(song.cover);
  if(coverUrl){
    const cover=appendText(header,"img","","detail-cover");
    cover.alt="";cover.loading="lazy";
    cover.onerror=()=>{
      cover.replaceWith(appendText(header,"div","♫","detail-cover detail-placeholder"));
    };
    cover.src=coverUrl;
  }else appendText(header,"div","♫","detail-cover detail-placeholder");
  const info=appendText(header,"div","","detail-info");
  appendText(info,"h2",song.title);
  const metadata=appendText(info,"div","","detail-meta");
  appendText(metadata,"span",song.uploader||"未知作者");
  addSourceTag(metadata,song);
  appendText(info,"div","点歌者 "+song.nickname+"　"+duration(song.duration),"detail-byline");
  const state=appendText(panel,"div","","detail-state");
  appendText(state,"strong",songState(song));
  if(song.priority_next)appendText(state,"span","下一首优先","detail-note");
  if(!song.skip_subtitles&&song.prep_error&&
     ["unavailable","original"].includes(song.prep_state))
    appendText(state,"span",song.prep_error,"detail-note");
  detailMediaActions(panel,song);
  const actions=detailActions(song,isAdminPath());
  if(actions){
    const section=appendText(panel,"section","","detail-section");
    appendText(section,"h3",isAdminPath()?"歌曲管理":"歌曲操作");
    section.append(actions);
  }
  if(isAdminPath()){
    const names={download:"下载",extract:"提取音频",kim:"人声分离",
      align:"歌词对齐",subtitles:"字幕生成",render:"烧录"};
    const entries=Object.entries(names).filter(([name])=>song.stage_seconds?.[name]!=null);
    const running=["downloading","aligning"].includes(song.prep_state);
    if(entries.length||running||(song.prep_error&&!song.skip_subtitles)){
      const details=appendText(panel,"details","","timing-details");
      details.open=timingWasOpen;
      appendText(details,"summary","制作详情");
      for(const [name,label] of entries){
        const line=appendText(details,"div","","timing-row");
        appendText(line,"span",label);
        appendText(line,"span",Number(song.stage_seconds[name]).toFixed(1)+" 秒");
      }
      if(running&&song.prep_stage_at){
        const line=appendText(details,"div","","timing-row");
        appendText(line,"span","当前阶段");
        appendText(line,"span",Math.max(0,Math.floor(Date.now()/1000-song.prep_stage_at))+" 秒","stage-current-time");
      }
      if(song.prep_error&&!song.skip_subtitles)appendText(details,"p",song.prep_error,"muted");
    }
  }
  if(isAdminPath()&&song.status==="ready"){
    const details=appendText(panel,"details","","timing-details");
    appendText(details,"summary","手工字幕参数");
    const label=appendText(details,"label","提前秒数");
    label.htmlFor="detail-lead-seconds";
    const lead=appendText(details,"input","");
    lead.id="detail-lead-seconds";lead.type="number";lead.min="0";
    lead.max="5";lead.step="0.1";lead.value=String(leadSeconds);
    lead.oninput=()=>{leadSeconds=Number(lead.value);};
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
  const health=await api("/api/health");
  gpuAlignment=health.gpu_alignment!==false;
  $("guest-lyrics-upload").hidden=!gpuAlignment;
  document.querySelector('label[for="guest-lyrics-upload"]').hidden=!gpuAlignment;
  $("netease-review-ai-card").hidden=!gpuAlignment;
  if(!health.auto_prepare){
    toast("后台自动制歌已关闭，可播放已有视频或请管理员手动上传",true);
  }else if(health.autoprep_ready===false){
    toast("后台制歌暂不可用，服务正在尝试恢复；已有视频仍可播放",true);
  }
  selectingSongId=songId;
  $("guest-lyrics-song").textContent=title;
  // Server supplies the same extracted name used for the backend search.
  // Show it immediately so the guest can correct an incorrect video title.
  $("guest-lyrics-query").value=(query||title).slice(0,60);
  if(!$("guest-lyrics-dialog").open)$("guest-lyrics-dialog").showModal();
  await searchGuestLyrics();
}
let suspendingLyricPicker=false;
function openNeteaseReview(candidate, useLyrics){
  const dialog=$("netease-review-dialog");
  if(dialog.open)return;
  const original=candidate.unfiltered_text||candidate.text;
  const lines=original.split("\n");
  // Existing cleanup only removes leading production credits.
  const defaultLines=candidate.text.split("\n");
  const removed=Math.max(0,lines.length-defaultLines.length);
  const chosen=new Set(lines.map((_,i)=>i).filter(i=>i>=removed));
  const rows=$("netease-review-lines");
  rows.replaceChildren();
  const count=$("netease-review-count");
  const update=()=>{
    count.textContent=`✓ 已保留 ${chosen.size} / ${lines.length} 行`;
    $("netease-review-select-all").disabled=chosen.size===lines.length;
    for(const button of rows.children){
      const kept=chosen.has(Number(button.dataset.index));
      button.classList.toggle("kept",kept);
      button.setAttribute("aria-pressed",String(kept));
      button.querySelector(".review-check").textContent=kept?"✓":"✕";
    }
  };
  lines.forEach((line,i)=>{
    const row=appendText(rows,"button","","netease-review-line");
    appendText(row,"span","","review-check").setAttribute("aria-hidden","true");
    appendText(row,"span",line,"review-line-text");
    row.type="button";row.dataset.index=String(i);
    row.onclick=()=>{if(chosen.has(i))chosen.delete(i);else chosen.add(i);update();};
  });
  $("netease-review-select-all").onclick=()=>{lines.forEach((_,i)=>chosen.add(i));update();};
  update();
  const providerAvailable=Boolean(candidate.timed_lrc&&candidate.timing_ticket);
  $("netease-review-provider").hidden=!providerAvailable;
  $("netease-review-ai-card").hidden=!gpuAlignment;
  showNeteaseSection("netease-line-review");
  dialog.querySelector(".alignment-cards").classList.toggle("single-mode",!providerAvailable||!gpuAlignment);
  $("netease-review-offset").value="0";
  document.querySelector(".offset-details").open=false;
  const options=()=>({
    selection_text:original,
    selection_ticket:candidate.unfiltered_ticket||candidate.ticket,
    selected_lines:[...chosen].sort((a,b)=>a-b)
  });
  const submit=(button,timed)=>{
    if(chosen.size<2){toast("至少保留两行歌词",true);return;}
    const extra=options();
    if(timed){
      const offset=$("netease-review-offset").value;
      const seconds=Number(offset);
      if(!offset||!Number.isFinite(seconds)){
        toast("请输入有效的时间偏移",true);return;
      }
      Object.assign(extra,{
        timing_mode:"provider",timed_lrc:candidate.timed_lrc,
        source_lrc_raw:candidate.source_lrc_raw,timing_ticket:candidate.timing_ticket,
        offset_ms:Math.round(seconds*1000)
      });
    }
    useLyrics(candidate.text,candidate.ticket,button,extra);
  };
  $("netease-review-align").onclick=()=>submit($("netease-review-align"),false);
  $("netease-review-timed").onclick=()=>submit($("netease-review-timed"),true);
  $("netease-review-back").onclick=()=>dialog.close();
  const picker=$("guest-lyrics-dialog");
  if(picker.open){
    suspendingLyricPicker=true;
    picker.close();
  }
  dialog.showModal();
}
// One section always owns the available height; the other is a compact header.
function showNeteaseSection(active){
  for(const [sectionId,contentId] of [
    ["netease-line-review","netease-review-content"],
    ["netease-alignment-choice","netease-alignment-cards"]
  ]){
    const expanded=sectionId===active;
    $(sectionId).classList.toggle("is-open",expanded);
    $(contentId).hidden=!expanded;
    $(sectionId).querySelector(".netease-accordion-toggle")
      .setAttribute("aria-expanded",String(expanded));
  }
}
for(const id of ["netease-line-review","netease-alignment-choice"]){
  $(id).querySelector(".netease-accordion-toggle").onclick=()=>showNeteaseSection(id);
}
$("netease-review-dialog").addEventListener("close",()=>{
  // Back button and Esc restore the same candidate list, without re-searching.
  if(selectingSongId!==null&&!$("guest-lyrics-dialog").open){
    $("guest-lyrics-dialog").showModal();
  }
});
function addLyricCandidates(candidates, songId, sequence){
  if(sequence!==selectionRequest || selectingSongId!==songId)return;
  const panel=$("guest-lyrics-results");
  candidates.forEach(candidate=>{
      if(!gpuAlignment && !(candidate.timed_lrc && candidate.timing_ticket))return;
    // The same lyrics from different recordings/sources may have different
    // timing. Deduplicating only by title/text can hide a usable fast route.
    const key=[candidate.source,candidate.url,candidate.text].join("\u0000");
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
    const useLyrics=(text,ticket,button,extra={})=>guarded(async()=>{
      await api("/api/songs/"+songId+"/lyrics/select","POST",{
        text,ticket,source:candidate.source,...extra
      });
      selectionRequest++;
      abortLyricRequests();
      selectingSongId=null;
      if($("netease-review-dialog").open)$("netease-review-dialog").close();
      if($("guest-lyrics-dialog").open)$("guest-lyrics-dialog").close();
      toast("✓ 歌词已选择");
      await refresh();
    },button);
    if(candidate.source==="网易云音乐"){
      const review=appendText(card,"button","逐行校正并使用","btn primary");
      review.type="button";
      review.onclick=()=>{
        try{openNeteaseReview(candidate,useLyrics);}catch(e){error(e);}
      };
      card.append(review);
      panel.append(card);
      return;
    }
    if(gpuAlignment){
      const button=appendText(card,"button","使用这一版歌词","btn primary");
      button.onclick=()=>useLyrics(candidate.text,candidate.ticket,button);
    }
    if(candidate.timed_lrc && candidate.timing_ticket){
      const timed=document.createElement("details");
      timed.open=!gpuAlignment;
      appendText(timed,"summary","⚡ 使用来源原时间轴（更快）");
      appendText(timed,"div","适合与网易云音乐等来源的同一音源，不适合演唱会或改编版。逐字渐变为等分估算；需要适应翻唱请选普通歌词对齐。","muted");
      const preview=candidate.timed_lrc.split("\n");
      appendText(timed,"pre",[...preview.slice(0,3),...(preview.length>4?["…",preview.at(-1)]:preview.slice(3))].join("\n"));
      const label=appendText(timed,"label","偏移（秒，视频比歌词晚则填正数）");
      const offset=document.createElement("input");
      offset.type="number";offset.step="0.1";offset.value="0";
      offset.setAttribute("aria-label","歌词时间偏移，单位秒");
      label.append(offset);
      const useTimed=appendText(timed,"button","沿用逐句时间轴","btn");
      useTimed.onclick=()=>{
        const value=Number(offset.value);
        if(!offset.value||!Number.isFinite(value)){
          toast("请输入有效的时间偏移");return;
        }
        useLyrics(candidate.text,candidate.ticket,useTimed,{
          timing_mode:"provider",timed_lrc:candidate.timed_lrc,
          source_lrc_raw:candidate.source_lrc_raw,timing_ticket:candidate.timing_ticket,
          offset_ms:Math.round(value*1000)
        });
      };
      card.append(timed);
    }
    if(gpuAlignment && candidate.unfiltered_text && candidate.unfiltered_ticket){
      const original=document.createElement("details");
      appendText(original,"summary","查看清理前的制作信息");
      appendText(original,"pre",candidate.unfiltered_text);
      const restore=appendText(original,"button","使用清理前版本","btn");
      restore.onclick=()=>useLyrics(candidate.unfiltered_text,candidate.unfiltered_ticket,restore);
      card.append(original);
    }
    panel.append(card);
  });
  updateLyricSearchStatus();
}
function updateLyricSearchStatus(){
  $("guest-lyrics-status").textContent=lyricHits?
    `已有 ${lyricHits} 版可选${lyricPending?"，其他来源仍在查询":""}`:
    (lyricPending?"正在查找歌词……":gpuAlignment?
      "没有找到候选，可以更换关键词或上传 LRC":
      "当前只能使用带时间轴的歌词，可换关键词或选择不加字幕");
}
async function fetchLyricPhase(phase, songId, query, sequence){
  const controller=new AbortController();
  lyricControllers.push(controller);
  lyricPending++;
  const deadline=setTimeout(()=>controller.abort(),20000);
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
        appendText($("guest-lyrics-results"),"div",gpuAlignment?
          "暂无歌词候选，可修改关键词或上传 LRC":
          "未找到带逐句时间轴的歌词，可修改关键词或直接使用原片","empty");
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
  if(suspendingLyricPicker){suspendingLyricPicker=false;return;}
  selectingSongId=null;selectionRequest++;abortLyricRequests();
});
$("guest-lyrics-skip").onclick=()=>guarded(async()=>{
  const songId=selectingSongId;
  if(songId===null)return;
  await api(`/api/songs/${songId}/lyrics/skip`,"POST");
  $("guest-lyrics-dialog").close();
  toast("✓ 已选择使用原片，不制作字幕");
  await refresh();
},$("guest-lyrics-skip"));
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
// Source selection changes only the input panel; both sources share the same queue.
function selectSongSource(source){
  for(const name of ["bilibili","netease"]){
    const active=name===source;
    $("source-tab-"+name).setAttribute("aria-selected",String(active));
    $("source-tab-"+name).tabIndex=active?0:-1;
    $("source-panel-"+name).hidden=!active;
  }
}
for(const source of ["bilibili","netease"]){
  $("source-tab-"+source).onclick=()=>selectSongSource(source);
  $("source-tab-"+source).addEventListener("keydown",(event)=>{
    if(event.key!=="ArrowLeft"&&event.key!=="ArrowRight")return;
    event.preventDefault();
    const next=source==="bilibili"?"netease":"bilibili";
    selectSongSource(next);
    $("source-tab-"+next).focus();
  });
}
let neteaseSearchVersion=0;
let neteasePage=0;
let neteaseLastQuery="";
let neteaseCanLoadMore=false;
async function submitNeteaseSong(ref,button){
  await guarded(async()=>{
    const result=await api("/api/netease/songs","POST",{ref});
    toast("✓ 已加入队列，请选择歌词");
    await refresh();
    await openLyricPicker(result.id,result.title,result.lyric_query);
  },button);
}
function renderNeteaseResults(songs, append=false){
  const list=$("netease-search-results");
  if(!append)list.replaceChildren();
  for(const song of songs){
    const item=appendText(list,"div","","netease-search-result");
    if(song.cover){
      const cover=document.createElement("img");
      cover.src=song.cover;
      cover.alt="";
      cover.loading="lazy";
      cover.referrerPolicy="no-referrer";
      item.append(cover);
    }else appendText(item,"span","🎵","netease-fallback-cover");
    const info=appendText(item,"div","","netease-search-result-info");
    appendText(info,"strong",song.title);
    appendText(info,"small",song.uploader+"  "+duration(song.duration));
    const choose=appendText(item,"button","点这首","btn neutral");
    choose.type="button";
    choose.onclick=()=>submitNeteaseSong(song.source_id,choose);
  }
}
async function queryNeteaseSongs(){
  const query=$("netease-song-query").value.trim();
  if(!query)throw new Error("请输入歌名、歌手或网易云分享链接");
  const version=++neteaseSearchVersion;
  const message=$("netease-search-message");
  message.textContent="正在查找歌曲……";
  $("netease-search-results").replaceChildren();
  try{
    const looksLikeShare=/^\d+$/.test(query)||/https?:\/\//i.test(query);
    const data=looksLikeShare?
      await api("/api/netease/preview?ref="+encodeURIComponent(query)):
      await api("/api/netease/search?q="+encodeURIComponent(query));
    if(version!==neteaseSearchVersion)return;
    const songs=looksLikeShare?[data]:data.songs;
    neteaseLastQuery=looksLikeShare?"":query;
    neteasePage=0;
    neteaseCanLoadMore=!looksLikeShare&&songs.length===30;
    renderNeteaseResults(songs);
    showNeteaseMore();
    message.textContent=songs.length?
      (looksLikeShare?"已识别分享歌曲，请确认歌名与歌手。":"找到 "+songs.length+" 首，请选择正确版本。"):
      "没有找到匹配歌曲，可以换歌名、歌手或粘贴分享链接。";
  }catch(e){
    if(version!==neteaseSearchVersion)return;
    message.textContent=e.message||"搜索失败";
    error(e);
  }
}
function showNeteaseMore(){
  const results=$("netease-search-results");
  results.querySelector(".netease-load-more")?.remove();
  if(!neteaseCanLoadMore)return;
  const button=appendText(results,"button","更多结果","btn netease-load-more");
  button.type="button";
  button.onclick=()=>guarded(async()=>{
    const version=neteaseSearchVersion;
    const next=neteasePage+1;
    const data=await api("/api/netease/search?q="+encodeURIComponent(neteaseLastQuery)+"&page="+next);
    if(version!==neteaseSearchVersion)return;
    neteasePage=next;
    neteaseCanLoadMore=data.songs.length===30;
    button.remove();
    renderNeteaseResults(data.songs,true);
    showNeteaseMore();
  },button);
}
$("netease-search-form").addEventListener("submit",(e)=>{
  e.preventDefault();
  guarded(queryNeteaseSongs,$("netease-search-btn"));
});
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
  "settings-queue":["点歌与队列","event-queue-settings-panel"],
  "settings-event":["场次管理","event-management"],
  "settings-accounts":["管理员账号","admin-accounts-panel"],
  "settings-netease":["网易云音乐连接","netease-admin-panel"],
  "settings-history":["历史歌单","event-archive-panel"],
  stats:["演唱统计","performance-panel"]
};
function setupSubpages(){
  const root=$("subpage-content");
  const menu=document.createElement("nav");menu.className="panel page-links";
  for(const [route,label,description] of [
    ["settings-playback","播放与视频","设备、字幕、画质"],
    ["settings-queue","点歌与队列","顺序、点歌权限"],
    ["settings-event","场次管理","加入码与场次"],
    ["settings-accounts","管理员账号","账号与权限"],
    ["settings-netease","网易云音乐","音乐源连接"],
    ["settings-history","历史歌单","记录与导出"]
  ]){
    const link=appendText(menu,"a","","settings-nav-item");
    link.href="#"+route;
    const copy=appendText(link,"span","","settings-nav-copy");
    appendText(copy,"strong",label);
    appendText(copy,"small",description);
    appendText(link,"span","›","settings-nav-chevron").setAttribute("aria-hidden","true");
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
  if(page!=="settings-netease"){
    stopNeteaseLoginPolling();
    neteaseLoginPageActive=false;
  }
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
document.querySelectorAll("[data-password-target]").forEach(button=>{
  button.onclick=()=>{
    const field=$(button.dataset.passwordTarget);
    const revealing=field.type==="password";
    field.type=revealing?"text":"password";
    button.textContent=revealing?"隐藏":"显示";
    button.setAttribute("aria-pressed",String(revealing));
    button.setAttribute("aria-label",revealing?"隐藏密码":"显示密码");
    field.focus();
  };
});
$("show-user-login").onclick=()=>{
  $("auth-register").classList.add("hidden");$("auth-login").classList.remove("hidden");
};
$("show-user-register").onclick=()=>{
  $("auth-login").classList.add("hidden");$("auth-register").classList.remove("hidden");
};
$("login-box").addEventListener("submit",(e)=>{e.preventDefault();guarded(async()=>{
  await api("/api/admin/login","POST",{
    username:$("admin-username").value.trim()||null,
    password:$("password").value
  });
  $("password").value="";admin=true;adminStateKey=null;toast("管理员登录成功");await refresh();
},$("login-btn"));});
function updateAccount(signedIn,name,role){
  const menu=$("account-menu");
  menu.classList.toggle("hidden",!signedIn);
  if(!signedIn){menu.open=false;return;}
  $("account-name").textContent=name;
  $("account-role").textContent=role;
}
$("account-logout").onclick=()=>guarded(async()=>{
  if(isAdminPath()){
    if(playbackMode==="browser"&&currentAdminSong){
      if(!confirm("当前歌曲正在播放，退出将跳过它。确定退出？"))return;
      await finishActive("skip",false);
    }
    await api("/api/admin/logout","POST");
    admin=false;adminStateKey=null;currentMediaId=null;
    player.pause();player.removeAttribute("src");player.load();
  }else await api("/api/users/logout","POST");
  $("account-menu").open=false;
  updateAccount(false,"","");
  await refresh();
},$("account-logout"));
let tvPrimaryState=null;
async function refreshTVPrimary(){
  const tv=await api("/api/admin/tv");
  tvPrimaryState=tv;
  const controls=$("tv-primary-management");
  controls.classList.toggle("hidden",playbackMode!=="tv");
  tvRemoteState=playbackMode==="tv"&&tv.active?
    await api("/api/admin/tv/remote"):null;
  const status=$("tv-primary-status");
  // Buttons already show whether a primary exists. Show ownership only
  // when it explains why this administrator cannot manage the screen.
  status.classList.toggle("hidden",!tv.active||tv.can_manage);
  if(tv.active&&!tv.can_manage)status.textContent="由全局管理员创建";
  const showActions=tv.can_manage;
  for(const id of ["tv-link-btn","tv-copy-btn"]){
    $(id).classList.toggle("hidden",!showActions);
  }
  $("tv-revoke-btn").classList.toggle("hidden",!showActions||!tv.active);
  $("tv-link-btn").textContent=tv.active?"▣ 更换主屏":"▣ 打开主屏";
  $("tv-copy-btn").textContent=tv.active?"⧉ 复制更换链接":"⧉ 复制主屏链接";
}
async function issueTVLink(){
  if(!tvPrimaryState?.can_manage)throw new Error("没有管理主屏的权限");
  return (await api("/api/admin/tv-link?replace="+String(tvPrimaryState.active))).url;
}
async function copyTVLink(link){
  const absolute=new URL(link,window.location.href).href;
  try{
    await navigator.clipboard.writeText(absolute);
    toast("链接已复制");
  }catch{prompt("复制链接",absolute);}
}
$("tv-link-btn").onclick=()=>{
  const tab=window.open("about:blank","_blank");
  guarded(async()=>{
    try{
      const link=await issueTVLink();
      if(!link){tab?.close();return;}
      if(tab)tab.location.href=link;
      else window.location.href=link;
    }catch(e){tab?.close();throw e;}
  },$("tv-link-btn"));
};
$("tv-copy-btn").onclick=()=>guarded(async()=>{
  const link=await issueTVLink();
  if(link)await copyTVLink(link);
},$("tv-copy-btn"));
$("tv-revoke-btn").onclick=()=>guarded(async()=>{
  if(!confirm("撤回主屏？当前歌曲保留，等待重新连接。"))return;
  await api("/api/admin/tv/revoke","POST");
  await refreshTVPrimary();
  toast("主屏已撤回");
},$("tv-revoke-btn"));
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

function clock(seconds){
  return Number.isFinite(seconds)?Math.floor(seconds/60)+":"+
    String(Math.floor(seconds%60)).padStart(2,"0"):"0:00";
}
function updateRemoteControls(){
  const isTV=playbackMode==="tv";
  const isBrowser=playbackMode==="browser";
  const status=tvRemoteState?.report||{};
  const online=Boolean(isTV&&tvRemoteState?.online);
  const canTV=Boolean(online&&tvRemoteState?.can_manage&&!tvRemoteState?.pending);
  const songId=currentAdminSong?.id||null;
  const tvPlaying=online&&status.active_id===songId&&Boolean(songId);
  const playable=Boolean(songId&&(isBrowser?currentMediaId===songId:isTV?tvPlaying: false));
  const remoteReady=isTV?canTV&&playable:playable;
  for(const name of ["remote-pause","remote-restart","remote-skip","remote-replay"]){
    const button=$(name);
    button.disabled=isTV?!remoteReady:(!isBrowser||!playable);
  }
  if(playbackMode==="manual"&&songId)$("remote-replay").disabled=false;
  const paused=isTV?Boolean(status.paused):player.paused;
  const pause=$("remote-pause");
  pause.title=paused?"继续":"暂停";
  pause.setAttribute("aria-label",pause.title);
  pause.querySelector("img").src="/icons/"+(paused?"play":"pause")+".svg";
  $("remote-progress").textContent=isTV?
    clock(status.position)+ " / "+clock(status.duration):
    clock(player.currentTime)+" / "+clock(player.duration);
  $("remote-progress").classList.toggle("hidden",playbackMode==="manual");
  const volume=$("remote-volume");
  const hasVolume=isTV?canTV:isBrowser;
  volume.disabled=!hasVolume;
  volume.closest(".remote-range").classList.toggle("hidden",playbackMode==="manual");
  if(document.activeElement!==volume)volume.value=String(isTV?(status.volume??100):Math.round(player.volume*100));
  $("tv-remote-extra").classList.toggle("hidden",!isTV);
  for(const button of document.querySelectorAll("[data-admin-offset]"))
    button.disabled=!canTV||!tvPlaying||!status.overlay;
  const offs=Number(status.offset||0);
  $("remote-offset-value").textContent=(offs>=0?"+":"")+(offs/10).toFixed(1)+"s";
  const opacity=$("remote-opacity");
  opacity.disabled=!canTV;
  if(document.activeElement!==opacity)opacity.value=String(status.opacity??15);
  const smooth=$("remote-smooth");
  smooth.disabled=!canTV;smooth.checked=Boolean(status.smooth);
  const indicator=$("tv-online");
  indicator.textContent=online?"主屏在线":tvPrimaryState?.active?"主屏离线":"主屏未连接";
  indicator.classList.toggle("online",online);
  const displayTitle=$("tv-playing");
  displayTitle.textContent=currentAdminSong?.title||"等待播放";
  displayTitle.href=currentAdminSong?"#song-"+currentAdminSong.id:"#";
  displayTitle.tabIndex=currentAdminSong?0:-1;
  $("tv-coming").textContent=nextAdminSong?"下一首："+nextAdminSong.title:"";
  if(status.error&&isTV)$("remote-error").textContent=status.error;
  else $("remote-error").textContent="";
}
function updatePlaybackUI(){
  const isBrowser=playbackMode==="browser";
  const isTV=playbackMode==="tv";
  manualMode=playbackMode==="manual";
  $("playback-mode").value=playbackMode;
  $("active-playback-mode").textContent=
    isTV?"电视大屏":manualMode?"相册播放":"浏览器播放";
  $("browser-player").classList.toggle("hidden",!isBrowser);
  $("tv-display").classList.toggle("hidden",!isTV);
  $("playing-title").classList.toggle("hidden",isTV);
  $("tv-primary-management").classList.toggle("hidden",!isTV);
  $("manual-controls").classList.toggle("hidden",!manualMode);
  $("legacy-admin-controls").classList.toggle("hidden",isTV);
  document.querySelector(".playback-remote").classList.toggle("manual",manualMode);
  $("autoplay-label").classList.toggle("hidden",!isBrowser);
  $("skip-btn").classList.toggle("hidden",isBrowser);
  $("start-btn").textContent=manualMode?"领取下一首":"开始播放";
  $("finish-btn").textContent=manualMode?"唱完并切下一首":"唱完";
  $("start-btn").disabled=Boolean(currentAdminSong);
  $("finish-btn").disabled=!currentAdminSong;
  updateRemoteControls();
}
async function sendRemote(action,value=null){
  if(playbackMode!=="tv")throw Error("当前不是电视模式");
  const hasSong=["pause","resume","restart","skip","replay_next"].includes(action);
  await api("/api/admin/tv/remote","POST",{
    action,song_id:hasSong?currentAdminSong?.id:null,value
  });
  if(tvRemoteState)tvRemoteState.pending=true;
  updateRemoteControls();
}
async function remoteAction(button,action){
  button.disabled=true;
  try{await action();}catch(e){error(e);}
  finally{updateRemoteControls();}
}
$("remote-pause").onclick=()=>remoteAction($("remote-pause"),async()=>{
  if(playbackMode==="tv")await sendRemote(tvRemoteState?.report?.paused?"resume":"pause");
  else if(playbackMode==="browser"){
    if(player.paused)await player.play();else player.pause();
  }
});
$("remote-restart").onclick=()=>remoteAction($("remote-restart"),async()=>{
  if(playbackMode==="tv")await sendRemote("restart");
  else if(playbackMode==="browser"){player.currentTime=0;await player.play();}
});
$("remote-skip").onclick=()=>remoteAction($("remote-skip"),async()=>{
  if(playbackMode==="tv")await sendRemote("skip");
  else if(playbackMode==="browser")await finishActive("skip",true);
});
$("remote-replay").onclick=()=>remoteAction($("remote-replay"),async()=>{
  if(playbackMode==="tv")await sendRemote("replay_next");
  else if(currentAdminSong){
    await api("/api/admin/songs/"+currentAdminSong.id+"/replay-next","POST");
    toast("已安排下一首重唱");await refresh();
  }
});
$("remote-volume").oninput=()=>{
  if(playbackMode==="browser")player.volume=Number($("remote-volume").value)/100;
};
$("remote-volume").onchange=()=>remoteAction($("remote-volume"),async()=>{
  if(playbackMode==="tv")await sendRemote("volume",Number($("remote-volume").value));
});
for(const button of document.querySelectorAll("[data-admin-offset]")){
  button.onclick=()=>remoteAction(button,async()=>{
    const current=Number(tvRemoteState?.report?.offset||0);
    await sendRemote("offset",Math.max(-600,Math.min(600,current+Number(button.dataset.adminOffset))));
  });
}
$("remote-opacity").onchange=()=>remoteAction($("remote-opacity"),
  ()=>sendRemote("opacity",Number($("remote-opacity").value)));
$("remote-smooth").onchange=()=>remoteAction($("remote-smooth"),
  ()=>sendRemote("smooth",$("remote-smooth").checked));
player.addEventListener("timeupdate",()=>{if(playbackMode==="browser")updateRemoteControls();});
player.addEventListener("play",updateRemoteControls);
player.addEventListener("pause",updateRemoteControls);
$("playback-mode").onchange=()=>guarded(async()=>{
  const mode=$("playback-mode").value;
  try{
    await api("/api/admin/playback-mode","PUT",{mode});
    playbackMode=mode;
    manualMode=mode==="manual";
    currentAdminSong=null;
    currentMediaId=null;
    player.pause();player.removeAttribute("src");player.load();
    adminStateKey=null;updatePlaybackUI();await refresh();
  }catch(e){
    updatePlaybackUI();throw e;
  }
},$("playback-mode"));

async function playNext(){
  if(playbackMode==="tv")throw Error("主屏负责自动播放");
  const data=await api("/api/admin/next","POST");
  if(!data.active){ toast("当前没有已准备好的歌曲");await refresh();return; }
  await assignPlayer(data.active,true);
  if(manualMode)toast("已领取当前歌曲，请下载后手动投屏");
  await refresh();
}
function clearPlayingTitle(){
  const title=$("playing-title");
  title.textContent="当前没有正在播放的歌曲。";
  title.classList.remove("playing-link");
  title.removeAttribute("role");title.removeAttribute("tabindex");
  title.onclick=null;title.onkeydown=null;
}
function setPlayingTitle(prefix,song){
  const title=$("playing-title");
  title.replaceChildren(document.createTextNode(prefix+song.title+"  "+song.nickname+"  "));
  addSourceTag(title,song);
  title.classList.add("playing-link");
  title.tabIndex=0;title.setAttribute("role","link");
  title.onclick=()=>{window.location.hash="song-"+song.id;};
  title.onkeydown=(e)=>{if(e.key==="Enter"){e.preventDefault();title.click();}};
}
async function assignPlayer(active, start){
  if(!active || !active.media_url)return;
  if(playbackMode!=="browser"){
    currentMediaId=active.id;
    setPlayingTitle("当前歌曲：",active);
    updatePlaybackUI();
    return;
  }
  if(currentMediaId!==active.id){
    currentMediaId=active.id;
    player.src=active.media_url;
    player.load();
    $("player-empty").classList.add("hidden");
  }
  setPlayingTitle("正在播放：",active);
  if(start){
    try{await player.play();}catch{toast("浏览器阻止自动播放，请手动点击播放器的播放按钮",true);}
  }
}
async function finishActive(reason, auto){
  if(playbackMode==="tv")throw Error("主屏负责切歌");
  const songId=currentMediaId;
  if(!songId) {toast("没有正在播放的歌曲");return false;}
  if(reason!=="completed")player.pause();
  await api("/api/admin/finish","POST",{song_id:songId,reason:reason==="skip"?"skip":""});
  currentMediaId=null;
  player.removeAttribute("src");player.load();
  $("player-empty").classList.remove("hidden");
  clearPlayingTitle();
  updatePlaybackUI();
  if(auto && $("autoplay").checked && !manualMode)await playNext();
  else await refresh();
  return true;
}
player.addEventListener("ended",()=>{if(playbackMode==="browser")guarded(()=>finishActive("completed",true));});
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
let neteaseLoginPolling=null;
let neteaseLoginChecking=false;
let neteaseLoginPageActive=false;
function stopNeteaseLoginPolling(){
  if(neteaseLoginPolling!==null)clearInterval(neteaseLoginPolling);
  neteaseLoginPolling=null;
}
function displayNeteaseLoginStatus(state){
  const connected=Boolean(state.connected);
  const pending=Boolean(state.pending)&&!connected;
  $("netease-login-start").classList.toggle("hidden",connected||pending);
  $("netease-login-disconnect").classList.toggle("hidden",!connected&&!pending);
  $("netease-login-check").classList.add("hidden");
  if(!pending)$("netease-login-qr-box").classList.add("hidden");
  $("netease-login-status").textContent=connected?
    "✓ 已保存网易云登录态（服务重启后可继续使用）":
    state.phase==="verification_required"?
      "网易云要求安全验证（8821）。本次扫码已暂停；可在官方网页正常登录，再使用下方网页登录态连接。":
    pending?(state.phase==="confirm"?"请在网易云音乐 App 中确认登录":"等待扫码确认……"):
    state.phase==="expired"?"二维码已过期，请重新扫码":"尚未连接网易云";
  if(connected||!pending||state.phase==="verification_required")stopNeteaseLoginPolling();
}
async function refreshNeteaseLogin(){
  const state=await api("/api/admin/netease/status");
  displayNeteaseLoginStatus(state);
  if(state.pending&&state.phase!=="verification_required"){
    if($("netease-login-qr-image").getAttribute("src")){
      stopNeteaseLoginPolling();
      neteaseLoginPolling=setInterval(checkNeteaseLogin,2500);
    }else{
      // Another browser initiated the QR; cancel and start a new one here.
      $("netease-login-status").textContent="其他页面正在扫码，可取消后重新连接";
    }
  }
}
async function checkNeteaseLogin(){
  if(neteaseLoginChecking)return;
  neteaseLoginChecking=true;
  try{
    const state=await api("/api/admin/netease/check","POST");
    displayNeteaseLoginStatus(state);
    if(state.connected)toast("✓ 网易云音乐已连接");
    if(state.phase==="verification_required")$("netease-cookie-import-panel").open=true;
  }catch(e){
    stopNeteaseLoginPolling();
    $("netease-login-status").textContent=e.message||"扫码检查失败，请稍后重试";
    $("netease-login-check").classList.remove("hidden");
  }finally{neteaseLoginChecking=false;}
}
$("netease-cookie-import-form").addEventListener("submit",(event)=>{
  event.preventDefault();
  guarded(async()=>{
    const field=$("netease-cookie-import-value");
    const value=field.value.trim();
    if(!value)throw new Error("请提供 MUSIC_U");
    try{
      const state=await api("/api/admin/netease/import","POST",{music_u:value});
      stopNeteaseLoginPolling();
      $("netease-login-qr-image").removeAttribute("src");
      displayNeteaseLoginStatus(state);
      $("netease-cookie-import-panel").open=false;
      toast("✓ 网易云网页登录态已验证并保存");
    }finally{field.value="";}
  },$("netease-cookie-import-submit"));
});
$("netease-login-check").onclick=()=>guarded(checkNeteaseLogin,$("netease-login-check"));
$("netease-login-start").onclick=()=>guarded(async()=>{
  const state=await api("/api/admin/netease/qr","POST");
  displayNeteaseLoginStatus(state);
  if(state.qr_image){
    $("netease-login-qr-image").src=state.qr_image;
    $("netease-login-qr-box").classList.remove("hidden");
    stopNeteaseLoginPolling();
    neteaseLoginPolling=setInterval(checkNeteaseLogin,2500);
  }
},$("netease-login-start"));
$("netease-login-disconnect").onclick=()=>guarded(async()=>{
  stopNeteaseLoginPolling();
  const wasConnected=!$("netease-login-disconnect").classList.contains("hidden") && $("netease-login-qr-box").classList.contains("hidden");
  const state=await api("/api/admin/netease/disconnect","POST");
  $("netease-login-qr-image").removeAttribute("src");
  displayNeteaseLoginStatus(state);
  toast(wasConnected?"已断开网易云连接":"已取消扫码");
},$("netease-login-disconnect"));
async function refreshAdminAccounts(){
  const target=$("admin-account-list");
  const data=await api("/api/admin/accounts");
  target.replaceChildren();
  if(!data.accounts.length){appendText(target,"p","本场尚未创建管理员","muted");return;}
  data.accounts.forEach(account=>{
    const item=appendText(target,"details","","account-entry");
    appendText(item,"summary",account.username);
    const actions=appendText(item,"div","","account-entry-actions");
    const password=document.createElement("input");
    password.type="password";password.autocomplete="new-password";
    password.minLength=6;password.maxLength=72;password.placeholder="新密码";
    password.setAttribute("aria-label","重置 "+account.username+" 的密码");
    actions.append(password);
    const reset=appendText(actions,"button","重置密码","btn neutral");
    reset.type="button";
    reset.onclick=()=>guarded(async()=>{
      if(password.value.length<6)throw new Error("密码至少 6 个字符");
      await api("/api/admin/accounts/"+account.id+"/password","PUT",{password:password.value});
      password.value="";toast("密码已重置");
    },reset);
    const revoke=appendText(actions,"button","撤销账号","btn subtle");
    revoke.type="button";
    revoke.onclick=()=>guarded(async()=>{
      if(!confirm("撤销管理员「"+account.username+"」？"))return;
      await api("/api/admin/accounts/"+account.id,"DELETE");
      toast("账号已撤销");await refreshAdminAccounts();
    },revoke);
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
  $("event-queue-settings-panel").classList.toggle("hidden",!active);
  const canManageAccounts=Boolean(active&&data.role==="global");
  for(const route of ["settings-accounts","settings-netease"]){
    document.querySelector('a[href="#'+route+'"]')?.classList.toggle("hidden",data.role!=="global");
  }
  if(data.role!=="global"&&["settings-accounts","settings-netease"].includes(currentRoute()))location.hash="settings";
  $("admin-accounts-panel").classList.toggle("hidden",!canManageAccounts);
  $("netease-admin-panel").classList.toggle("hidden",data.role!=="global");
  if(data.role!=="global"){
    stopNeteaseLoginPolling();
    neteaseLoginPageActive=false;
  }else if(currentRoute()==="settings-netease"&&!neteaseLoginPageActive){
    neteaseLoginPageActive=true;
    await refreshNeteaseLogin();
  }
  if(adminAccountsEventId!==(canManageAccounts?active.id:null)){
    adminAccountsEventId=canManageAccounts?active.id:null;
    $("admin-account-list").replaceChildren();
    if(canManageAccounts&&$("admin-accounts-panel").open)await refreshAdminAccounts();
  }
  $("admin-role").textContent=data.role==="global"?"全局管理员":
    "本场管理员："+(data.username||"");
  updateAccount(true,data.role==="global"?"管理员":data.username||"管理员",
    data.role==="global"?"全局管理员":"本场管理员");

  const key=JSON.stringify([data.role,data.events.map(e=>
    [e.id,e.name,e.status,e.media_released_at])]);
  if(key!==archiveListKey){
    archiveListKey=key;
    const target=$("event-history");target.replaceChildren();
    if(!data.events.length)appendText(target,"p","暂无历史场次","muted");
    data.events.forEach(event=>{
      const item=appendText(target,"div","","archive-item");
      const info=appendText(item,"div","","archive-info");
      appendText(info,"strong",event.name);
      appendText(info,"small",event.id===data.active?"进行中":
        event.media_released_at?"已释放媒体":"已归档");
      const actions=appendText(item,"div","","archive-actions");
      const id=encodeURIComponent(event.id);
      const link=appendText(actions,"a","↓ CSV","btn neutral");
      link.href="/api/admin/events/"+id+"/songs.csv";
      if(data.role==="global"&&event.status==="closed"&&!event.legacy){
        if(!event.media_released_at){
          const release=appendText(actions,"button","释放空间","btn neutral");
          release.type="button";
          release.onclick=()=>guarded(async()=>{
            if(!confirm("释放「"+event.name+"」的视频等大文件？保留记录和文本。"))return;
            const result=await api("/api/admin/events/"+id+"/release-media","POST");
            toast("已释放 "+(result.freed_bytes/1048576).toFixed(1)+" MiB");
            archiveListKey=null;await refreshEvents();
          },release);
        }
        const remove=appendText(actions,"button","删除记录","btn subtle archive-delete");
        remove.type="button";
        remove.onclick=()=>guarded(async()=>{
          if(prompt("输入场次名称确认删除全部记录：", "")!==event.name)return;
          await api("/api/admin/events/"+id,"DELETE");
          toast("已删除场次");
          archiveListKey=null;await refreshEvents();
        },remove);
      }
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
    $("max-bandwidth-mbps").value=String(settings.max_bandwidth_mbps);
    $("pause-orders").checked=settings.orders_paused;
    $("tv-html-overlay").checked=settings.tv_html_overlay;
  }else if(document.activeElement!==$("queue-policy")&&
           document.activeElement!==$("allow-unsubtitled")&&
           document.activeElement!==$("subtitle-style")&&
           document.activeElement!==$("video-quality")&&
           document.activeElement!==$("max-bandwidth-mbps")&&
           document.activeElement!==$("pause-orders")&&
           document.activeElement!==$("tv-html-overlay")&&
           document.activeElement!==$("settings-save-btn")){
    $("queue-policy").value=settings.queue_policy;
    $("subtitle-style").value=settings.subtitle_style;
    $("video-quality").value=String(settings.video_quality);
    $("allow-unsubtitled").checked=settings.allow_unsubtitled;
    $("max-bandwidth-mbps").value=String(settings.max_bandwidth_mbps);
    $("pause-orders").checked=settings.orders_paused;
    $("tv-html-overlay").checked=settings.tv_html_overlay;
  }
  updateStylePreview();
}
function updateStylePreview(){
  $("style-preview").className="style-preview "+$("subtitle-style").value;
}
$("subtitle-style").onchange=updateStylePreview;
$("settings-save-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/settings","PUT",{
    subtitle_style:$("subtitle-style").value,
    video_quality:Number($("video-quality").value),
    max_bandwidth_mbps:Number($("max-bandwidth-mbps").value),
    tv_html_overlay:$("tv-html-overlay").checked
  });
  toast("已保存");await refresh();
},$("settings-save-btn"));
$("queue-settings-save").onclick=()=>guarded(async()=>{
  await api("/api/admin/settings","PUT",{
    queue_policy:$("queue-policy").value,
    allow_unsubtitled:$("allow-unsubtitled").checked,
    orders_paused:$("pause-orders").checked
  });
  adminStateKey=null;listKeys.clear();
  toast("已保存");await refresh();
},$("queue-settings-save"));
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
  if(!isAdminPath())updateAccount(entry.authenticated,entry.username||"","点歌用户");
  $("request-panel").classList.toggle("hidden",!entry.authenticated);
  $("queue-panel").classList.toggle("hidden",!entry.authenticated);

  showPage();
}
$("entry-code").addEventListener("submit",(e)=>{e.preventDefault();guarded(async()=>{
  await api("/api/join","POST",{code:$("join-code").value.trim()});
  $("join-code").value="";
  await refresh();
},$("join-btn"));});
$("register-form").addEventListener("submit",(e)=>{e.preventDefault();guarded(async()=>{
  await api("/api/users/register","POST",{
    username:$("register-name").value.trim(),
    password:$("register-password").value,
    confirm_password:$("register-confirm").value
  });
  $("register-password").value="";$("register-confirm").value="";
  toast("✓ 已进入");
  await refresh();
},$("register-btn"));});
$("user-login-form").addEventListener("submit",(e)=>{e.preventDefault();guarded(async()=>{
  await api("/api/users/login","POST",{
    username:$("user-login-name").value.trim(),
    password:$("user-login-password").value
  });
  $("user-login-password").value="";
  toast("登录成功");await refresh();
},$("user-login-btn"));});
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
        updateRemoteControls();
        if(currentRoute()==="played")await refreshSongHistory();
        if(/^song-[0-9]+$/.test(currentRoute()))await refreshSongDetail();
        if(guestStateKey!==stateKey){
          guestStateKey=stateKey;
          $("queue-count").textContent=state.queue.length+" 首可播";
          fillList("queue-ready",state.queue);
          fillList("queue-waiting",state.waiting);
          const current=$("current-guest");current.replaceChildren();
          if(state.current)current.append(compactSong(state.current,"▶"));
          else appendText(current,"p","暂无正在播放的歌曲");
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
      if(!admin)updateAccount(false,"","");
      $("admin-panel").classList.toggle("hidden",!admin||Boolean(adminRoutes[currentRoute()]||currentRoute()==="played"||/^song-[0-9]+$/.test(currentRoute())));
      $("subpage").classList.toggle("hidden",!admin||!(adminRoutes[currentRoute()]||currentRoute()==="played"||/^song-[0-9]+$/.test(currentRoute())));
      if(admin){
        await refreshEvents();
        const activeState=adm.active?{...adm.active,media_url:null}:null;
        playbackMode=adm.playback_mode||"tv";
        manualMode=playbackMode==="manual";
        currentAdminSong=adm.active||null;
        nextAdminSong=adm.queue?.[0]||null;
        await refreshTVPrimary();
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
          $("current-song-copy-source").classList.toggle("hidden",!adm.active||!songSource(adm.active).ref);
          if(adm.active){
            $("current-song-download").href=`/api/songs/${adm.active.id}/download`;
            $("current-song-copy-source").textContent="⧉ 复制"+songSource(adm.active).copyLabel;
            $("current-song-copy-source").onclick=()=>guarded(
              ()=>copySongSource(adm.active),$("current-song-copy-source"));
            if(currentMediaId!==adm.active.id)await assignPlayer(adm.active,false);
            setPlayingTitle(playbackMode==="browser"?"正在播放：":"当前歌曲：",adm.active);
          }else if(currentMediaId!==null){
            currentMediaId=null;player.pause();player.removeAttribute("src");player.load();
            $("player-empty").classList.remove("hidden");
            clearPlayingTitle();
            updatePlaybackUI();
          }
        }
        updateRemoteControls();
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
