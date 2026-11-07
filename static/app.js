import {setupAdminAccounts} from "./admin-accounts.js";
import {setupNeteaseConnection} from "./admin-netease.js";
import {setupTVPrimary} from "./tv-primary.js";
import {setupRemoteControls} from "./remote-controls.js";
import {setupSongRequests} from "./song-requests.js";
import {createLyricPicker} from "./lyrics.js";
"use strict";
const $ = (id) => document.getElementById(id);
let admin = false;
let currentMediaId = null;
let manualMode = false;
let playbackMode = "tv";
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
let currentEventId = null;
let leadSeconds = 1.8;
let archiveListKey = null;
let settingsEventId = null;
let adminAccountsEventId = null;
let neteaseLoginPageActive = false;
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
  if(lyricPicker.selectedSongId()===songId && $("guest-lyrics-dialog").open)$("guest-lyrics-dialog").close();
  if(currentRoute()==="song-"+songId)window.location.hash="";
  toast("✓ 已取消点歌");
  await refresh();
}
function songState(song) {
  if(song.status==="playing")return "播放中";
  if(song.status==="played")return "已唱";
  if(song.status==="skipped")return "已跳过";
  if(song.status==="cancelled")return "已取消";
  if(song.status==="closed_unplayed")return "散场未唱";
  if(song.status==="closed_interrupted")return "散场中断";
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
    if(["played","skipped"].includes(song.status)&&song.downloadable)
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
  const data=await api("/api/songs/history");
  const key=JSON.stringify(data);
  if(playedKey===key)return;
  playedKey=key;
  const target=$("page-played");target.replaceChildren();
  if(!data.songs.length){appendText(target,"p","暂无歌曲记录","empty");return;}
  const list=appendText(target,"div","","song-list");
  data.songs.forEach((song,index)=>{
    const item=compactSong(song,index+1);
    if(data.archived){
      item.disabled=true;item.onclick=null;
      item.setAttribute("aria-label",song.title+"，"+songState(song));
    }
    list.append(item);
  });
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

const lyricPicker = createLyricPicker({
  $, api, toast, error, guarded, appendText, refresh, cancelGuestSong
});
const {openLyricPicker} = lyricPicker;

setupSongRequests({
  $, api, guarded, toast, error, appendText, duration, refresh, openLyricPicker
});

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
  $("subpage-title").textContent=page==="played"?"本场记录":page==="song"?"歌曲详情":
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
  document.title="VC KTV | "+(page==="played"?"本场记录":page==="song"?"歌曲详情":
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
const primaryTV=setupTVPrimary({
  $, api, toast, guarded, isAdminPath,
  getPlayback:()=>({admin,playbackMode}),
  updateRemoteControls:()=>updateRemoteControls()
});
const {refreshTVPrimary,pollTVRemoteStatus}=primaryTV;
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

const {updateRemoteControls} = setupRemoteControls({
  $, api, error, toast, refresh, finishActive, player,
  getState:()=>({playbackMode,currentAdminSong,nextAdminSong,currentMediaId,
    ...primaryTV.state()})
});
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
const {stopNeteaseLoginPolling,refreshNeteaseLogin} = setupNeteaseConnection({
  $, api, guarded, toast
});
const {refreshAdminAccounts} = setupAdminAccounts({
  $, api, guarded, toast, appendText
});
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
      const view=appendText(actions,"button","查看","btn neutral");
      view.type="button";
      const details=appendText(item,"div","","archive-songs hidden");
      view.onclick=()=>guarded(async()=>{
        if(!details.classList.contains("hidden")){
          details.classList.add("hidden");
          view.textContent="查看";
          return;
        }
        const data=await api("/api/admin/events/"+id+"/songs");
        details.replaceChildren();
        if(!data.songs.length)appendText(details,"p","暂无歌曲","muted");
        data.songs.forEach((song,index)=>{
          const row=appendText(details,"div","","archive-song");
          appendText(row,"span",String(index+1),"archive-number");
          const copy=appendText(row,"span","","archive-song-copy");
          appendText(copy,"strong",song.title);
          const metadata=appendText(copy,"span","","song-meta");
          appendText(metadata,"span",song.requester,"song-requester");
          appendText(metadata,"span","|","song-meta-separator");
          appendText(metadata,"span",song.uploader||"未知作者","song-author");
          addSourceTag(metadata,song);
          appendText(row,"span",songState(song),"song-state");
        });
        details.classList.remove("hidden");
        view.textContent="收起";
      },view);
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
  if(!confirm("结束本场？正在播放的歌曲将中断，未唱歌曲会保留记录。"))return;
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
    ?entry.archived_event?"已散场："+entry.archived_event.name:"暂无活动场次"
    :entry.joined
      ?entry.event.name
      :entry.event.name+"  输入六位加入码";
  $("entry-code").classList.toggle("hidden",!entry.event||entry.joined);
  $("entry-account").classList.toggle("hidden",!entry.event||!entry.joined||entry.authenticated);
  const pastOnly=Boolean(!entry.authenticated&&entry.archived_event);
  guestAuthenticated=Boolean(entry.authenticated||
    (pastOnly&&(!entry.event||currentRoute()==="played")));
  $("guest-access").classList.toggle("hidden",guestAuthenticated);
  $("guest-past-link").classList.toggle("hidden",!pastOnly||!entry.event);
  if(!isAdminPath())updateAccount(guestAuthenticated,
    entry.username||entry.archived_event?.username||"","点歌用户");
  $("request-panel").classList.toggle("hidden",!entry.authenticated);
  $("queue-panel").classList.toggle("hidden",!entry.authenticated);
  if(entry.archived_event&&!entry.event&&currentRoute()!=="played")
    window.location.hash="played";
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
        if(entry.archived_event&&currentRoute()==="played")await refreshSongHistory();
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
setInterval(pollTVRemoteStatus,1000);
document.addEventListener("visibilitychange",()=>{
  if(document.visibilityState==="visible")refresh();
});
window.addEventListener("online",()=>refresh());
