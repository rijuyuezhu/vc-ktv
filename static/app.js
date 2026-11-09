import {setupSongDetail} from "./song-detail.js";
import {setupAdminEvents} from "./admin-events.js";
import {setupTVPrimary} from "./tv-primary.js";
import {setupRemoteControls} from "./remote-controls.js";
import {setupSongRequests} from "./song-requests.js";
import {createLyricPicker} from "./lyrics.js";
import {$, api, toast, error, guarded, duration, appendText} from "./ui-common.js";
"use strict";
let admin = false;
let currentMediaId = null;
let manualMode = false;
let playbackMode = "tv";
let currentAdminSong = null;
let nextAdminSong = null;
let refreshing = false;
let refreshQueued = false;
let refreshFailed = false;
let guestStateKey = null;
let adminStateKey = null;
const listKeys = new Map();
let currentEventId = null;
let guestAuthenticated = false;
let playedKey = null;
const player = $("player");


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
const lyricPicker = createLyricPicker({refresh, cancelGuestSong});
const {openLyricPicker} = lyricPicker;

setupSongRequests({refresh, openLyricPicker});
const {refreshSongDetail,invalidateSongDetail}=setupSongDetail({
  refresh, currentRoute, isAdminPath, songState, addSourceTag,
  openLyricPicker, cancelGuestSong
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
    leaveNeteasePage();
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
  invalidateSongDetail();playedKey=null;showPage();refresh();
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
  isAdminPath,
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
  refresh, finishActive, player,
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
const {refreshEvents,leaveNeteasePage}=setupAdminEvents({
  refresh, currentRoute, updateAccount, addSourceTag, songState,
  onEventChanged:()=>{invalidateSongDetail();playedKey=null;},
  invalidateQueueState:()=>{adminStateKey=null;listKeys.clear();}
});

function renderEntry(entry){
  if(entry.event?.id!==currentEventId){
    invalidateSongDetail();playedKey=null;
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
