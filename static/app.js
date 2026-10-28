"use strict";
const $ = (id) => document.getElementById(id);
let preview = null;
let admin = false;
let currentMediaId = null;
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
let currentEventId = null;
let archiveListKey = null;
let settingsEventId = null;
const player = $("player");


function toast(message, error=false) {
  clearTimeout(toastTimer);
  const t = $("toast"); t.textContent = message;
  t.className = "show" + (error ? " error" : "");
  toastTimer = setTimeout(() => { t.className = ""; }, 4800);
}
async function api(url, method="GET", body=null) {
  let opts = {method, credentials:"same-origin", cache:"no-store"};
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
function makeSong(song, order, mode="guest") {
  const outer = document.createElement("div");outer.className="song-item";
  appendText(outer,"span",String(order),"ordinal");
  const copy = document.createElement("div");copy.className="song-copy";outer.append(copy);
  appendText(copy,"strong",song.title+(song.page>1?` (P${song.page})`:""));
  appendText(copy,"small",`${song.nickname}  ${duration(song.duration)}  ${song.uploader}`);
  if(mode==="guest" && song.mine && ["ready","waiting"].includes(song.status) &&
     song.prep_state!=="aligning"){
    const pick=appendText(outer,"button",song.lyric_selected?"♫ 换歌词":"♫ 选歌词","mini lyric-pick");
    pick.onclick=()=>guarded(()=>openLyricPicker(song.id,song.title),pick);
  }
  if(mode==="status"){
    const states={
      pending:"◷ 等待下载",downloading:"↓ 下载中",
      downloaded:"◷ 等待歌词",aligning:"◷ 字幕制作中",done:"✓ 已准备",
      original:"! 字幕失败",unavailable:"! 下载失败",manual:"✓ 已准备"
    };
    const status=song.awaiting_subtitles
      ? (song.prep_state==="original"?"! 字幕失败":
         !song.lyric_selected?"◷ 等待歌词":"◷ 等待字幕")
      : (states[song.prep_state]||"◷ 准备中");
    const stateLabel=appendText(outer,"span",status,"status"+(status.startsWith("!")?" wait":""));
    if(song.prep_error)stateLabel.title=song.prep_error;
    if(song.prep_state==="unavailable"){
      const a=appendText(outer,"a","↗ B站");a.href="https://www.bilibili.com/video/"+song.bvid+"/?p="+song.page;a.target="_blank";a.rel="noopener noreferrer";
    }
  }else if(mode==="admin"){
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
    if(song.status==="ready" && song.prep_state==="original"){
      const retry=appendText(actions,"button","↻ 重试字幕","mini");
      retry.title=song.prep_error||"只重试字幕，保留原视频";
      retry.onclick=()=>guarded(async()=>{
        await api(`/api/admin/songs/${song.id}/subtitles/retry`,"POST");
        toast("◷ 已重新排队制作字幕");await refresh();
      },retry);
    }
    if(song.status==="ready"){
      const render=appendText(actions,"button","制作 KTV","mini");render.onclick=()=>guarded(async()=>{
        const lead=Number($("lead-seconds").value);
        await api(`/api/admin/songs/${song.id}/render`,"POST",{lead});
        toast("◷ 正在制作字幕");await refresh();
      },render);
    }else if(song.status==="rendering"){
      appendText(actions,"span","制作中","status wait");
    }
    if(song.status!=="rendering"){
      const cancel=appendText(actions,"button","取消","mini");cancel.onclick=()=>guarded(async()=>{
      if(!confirm("取消这首歌曲？"))return;
      await api(`/api/admin/songs/${song.id}/cancel`,"POST"); await refresh();
    },cancel);
    }
  }else{
    const status=song.awaiting_subtitles
      ? (song.prep_state==="original"?"! 字幕失败":
         song.lyric_selected?"◷ 等待字幕":"◷ 请选择歌词")
      : song.status==="ready"?"✓ 可播放":
        song.prep_state==="unavailable"?"! 暂不可用":"◷ 准备中";
    appendText(outer,"span",status,"status"+(status.startsWith("!")?" wait":""));
  }
  return outer;
}
function fillList(id, songs, mode="guest") {
  const key=JSON.stringify([mode,songs]);
  if(listKeys.get(id)===key)return;
  listKeys.set(id,key);
  const target=$(id);target.replaceChildren();
  if(!songs.length){ appendText(target,"div",mode==="admin"?"当前没有待处理的歌曲":"暂无歌曲","empty"); return; }
  songs.forEach((s,i)=>target.append(makeSong(s,i+1,mode)));
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
async function openLyricPicker(songId,title){
  selectingSongId=songId;
  $("guest-lyrics-song").textContent=title;
  $("guest-lyrics-query").value="";
  $("guest-lyrics-results").replaceChildren();
  if(!$("guest-lyrics-dialog").open)$("guest-lyrics-dialog").showModal();
  await searchGuestLyrics();
}
async function searchGuestLyrics(){
  const songId=selectingSongId;
  if(songId===null)return;
  const sequence=++selectionRequest;
  const panel=$("guest-lyrics-results");
  panel.replaceChildren();
  appendText(panel,"div","正在查找歌词候选……","empty");
  try{
    const query=$("guest-lyrics-query").value.trim();
    const data=await api("/api/songs/"+songId+"/lyrics/candidates"+(query?"?query="+encodeURIComponent(query):""));
    if(sequence!==selectionRequest || selectingSongId!==songId)return;
    if(!query)$("guest-lyrics-query").value=data.query;
    panel.replaceChildren();
    if(!data.candidates.length){
      appendText(panel,"div","没有搜到歌词。可以试试只输入歌名、去掉歌手名，或用歌曲别名再搜。","empty");
      return;
    }
    data.candidates.forEach((candidate)=>{
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
        selectingSongId=null;
        $("guest-lyrics-dialog").close();
        toast("✓ 歌词已选择");
        await refresh();
      },button);
      panel.append(card);
    });
  }catch(e){
    if(sequence!==selectionRequest)return;
    panel.replaceChildren();
    appendText(panel,"div",e.message||"歌词搜索失败，请再试一次","empty");
  }
}
$("guest-lyrics-dialog").addEventListener("close",()=>{selectingSongId=null;selectionRequest++;});
$("guest-lyrics-search").onclick=()=>guarded(()=>searchGuestLyrics(),$("guest-lyrics-search"));
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
  await openLyricPicker(result.id,result.title);
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
  await openLyricPicker(result.id,title);
},$("submit-btn"));
function showPage(){
  const isAdmin=window.location.pathname.replace(/\/$/, "")==="/admin";
  $("guest-view").classList.toggle("hidden",isAdmin);
  $("admin-view").classList.toggle("hidden",!isAdmin);
  $("tab-guest").classList.toggle("selected",!isAdmin);
  $("tab-admin").classList.toggle("selected",isAdmin);
  $("tab-guest").setAttribute("aria-current",isAdmin?"false":"page");
  $("tab-admin").setAttribute("aria-current",isAdmin?"page":"false");
  $("hero-title").textContent=isAdmin?"管理 KTV":"想唱什么？";
  $("hero-desc").textContent=isAdmin
    ?"控制播放与场次设置"
    :"输入视频链接，选好歌词就可以排队";
  document.title=isAdmin?"VC KTV | 管理":"VC KTV | 点歌";
}
$("admin-advanced").addEventListener("toggle",()=>{
  if($("admin-advanced").open){adminStateKey=null;refresh();}
});
$("login-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/login","POST",{password:$("password").value});
  $("password").value="";admin=true;adminStateKey=null;toast("管理员登录成功");await refresh();
},$("login-btn"));
$("password").onkeydown=(e)=>{if(e.key==="Enter")$("login-btn").click();};
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
$("finish-btn").onclick=()=>guarded(async()=>{await finishActive("completed",false);},$("finish-btn"));
$("skip-btn").onclick=()=>guarded(async()=>{await finishActive("skip",false);},$("skip-btn"));

async function playNext(){
  const data=await api("/api/admin/next","POST");
  if(!data.active){ toast("当前没有已准备好的歌曲");await refresh();return; }
  await assignPlayer(data.active,true);
  await refresh();
}
async function assignPlayer(active, start){
  if(!active || !active.media_url)return;
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
  if(!songId) {toast("没有正在播放的歌曲");return;}
  if(reason!=="completed")player.pause();
  await api("/api/admin/finish","POST",{song_id:songId,reason:reason==="skip"?"skip":""});
  currentMediaId=null;
  player.removeAttribute("src");player.load();
  $("player-empty").classList.remove("hidden");
  $("playing-title").textContent="当前没有正在播放的歌曲";
  if(auto && $("autoplay").checked)await playNext(); else await refresh();
}
player.addEventListener("ended",()=>guarded(()=>finishActive("completed",true)));
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
async function refreshEvents(){
  const data=await api("/api/admin/events");
  const active=data.events.find(e=>e.id===data.active);
  $("event-status").textContent=active?"♫ "+active.name:"尚未创建场次";
  $("event-join-code").textContent=active&&!active.legacy?"加入码  "+active.code:"";
  $("event-create-fields").classList.toggle("hidden",!!active);
  $("event-close-btn").classList.toggle("hidden",!active);
  $("event-settings-panel").classList.toggle("hidden",!active);

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
    $("allow-unsubtitled").checked=settings.allow_unsubtitled;
  }else if(document.activeElement!==$("queue-policy")&&
           document.activeElement!==$("allow-unsubtitled")&&
           document.activeElement!==$("settings-save-btn")){
    $("queue-policy").value=settings.queue_policy;
    $("allow-unsubtitled").checked=settings.allow_unsubtitled;
  }
}
$("settings-save-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/settings","PUT",{
    queue_policy:$("queue-policy").value,
    allow_unsubtitled:$("allow-unsubtitled").checked
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
    currentEventId=entry.event?.id||null;
    $("join-code").value="";
  }
  $("guest-entry-description").textContent=!entry.event
    ?"暂无活动场次"
    :entry.joined
      ?entry.event.name+"  选择注册或登录"
      :entry.event.name+"  输入六位加入码";
  $("entry-code").classList.toggle("hidden",!entry.event||entry.joined);
  $("entry-account").classList.toggle("hidden",!entry.event||!entry.joined||entry.authenticated);
  $("guest-access").classList.toggle("hidden",entry.authenticated);
  $("guest-profile").classList.toggle("hidden",!entry.authenticated);
  $("request-panel").classList.toggle("hidden",!entry.authenticated);
  $("queue-panel").classList.toggle("hidden",!entry.authenticated);
  $("guest-greeting").textContent=entry.authenticated?"当前用户："+entry.username+"  "+entry.event.name:"";
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
    const isGuest=!$("guest-view").classList.contains("hidden");
    if(isGuest){
      const entry=await api("/api/entry");
      renderEntry(entry);
      if(entry.authenticated){
        const state=await api("/api/state");
        const stateKey=JSON.stringify([entry.event?.id,state.current,state.queue,state.waiting]);
        if(guestStateKey!==stateKey){
          guestStateKey=stateKey;
          $("queue-count").textContent=state.queue.length+" 首可播";
          fillList("queue-ready",state.queue);
          fillList("queue-waiting",state.waiting);
          const current=$("current-guest");current.replaceChildren();
          appendText(current,"span","▶ 正在播放","current-label");
          if(state.current){
            appendText(current,"strong",state.current.title);
            appendText(current,"small","点歌者："+state.current.nickname);
          }else appendText(current,"p","当前空闲，等待管理员开始播放");
        }
      }else{
        guestStateKey=null;
      }
    }
    if(!$("admin-view").classList.contains("hidden")){
      let adm;
      try{adm=await api("/api/admin/state");admin=true;}
      catch(e){if(e.message.includes("管理员"))admin=false;else throw e;}
      $("login-box").classList.toggle("hidden",admin);
      $("admin-panel").classList.toggle("hidden",!admin);
      if(admin){
        await refreshEvents();
        const activeState=adm.active?{...adm.active,media_url:null}:null;
        const key=JSON.stringify([adm.waiting,adm.queue,adm.guests,activeState]);
        if(key!==adminStateKey){
          adminStateKey=key;
          const pending=[...adm.waiting,...adm.queue];
          fillList("manage-status",pending,"status");
          if($("admin-advanced").open)fillList("manage-list",pending,"admin");
          const stats=$("stats");stats.replaceChildren();
          if(!adm.guests.length)appendText(stats,"div","暂无演唱记录","empty");
          adm.guests.forEach(u=>{
            const c=document.createElement("div");c.className="stat";
            appendText(c,"strong",u.name);
            appendText(c,"small",u.plays+" 首  "+duration(u.seconds));stats.append(c);
          });
          if(adm.active){
            if(currentMediaId!==adm.active.id)await assignPlayer(adm.active,false);
            $("playing-title").textContent="正在播放："+adm.active.title+"  "+adm.active.nickname;
          }else if(currentMediaId!==null){
            currentMediaId=null;player.pause();player.removeAttribute("src");player.load();
            $("player-empty").classList.remove("hidden");
            $("playing-title").textContent="当前没有正在播放的歌曲";
          }
        }
      }else adminStateKey=null;
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
refresh();
setInterval(()=>{if(document.visibilityState==="visible")refresh();},3500);
document.addEventListener("visibilitychange",()=>{
  if(document.visibilityState==="visible")refresh();
});
window.addEventListener("online",()=>refresh());
