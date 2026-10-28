"use strict";
const $ = (id) => document.getElementById(id);
let preview = null;
let admin = false;
let currentMediaId = null;
let popupSongId = null;
let toastTimer = null;
let refreshing = false;
let selectingSongId = null;
let selectionRequest = 0;
const player = $("player");
$("nickname").value = localStorage.getItem("vc-ktv-nick") || "";

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
  appendText(copy,"small",`${song.nickname} · ${duration(song.duration)} · ${song.uploader}`);
  if(mode==="guest" && song.mine && ["ready","waiting"].includes(song.status) &&
     song.prep_state!=="aligning"){
    const pick=appendText(outer,"button",song.lyric_selected?"更换歌词":"选择歌词","mini lyric-pick");
    pick.onclick=()=>guarded(()=>openLyricPicker(song.id,song.title),pick);
  }
  if(mode==="status"){
    const states={pending:"等待自动准备",downloading:"下载视频中",downloaded:"原视频已就绪",
      aligning:"字幕生成中 · 原视频可播",done:"KTV 字幕已完成",original:"原视频可播",
      unavailable:song.prep_retry_at>Date.now()/1000?"暂不可获取 · 将自动重试":"暂不可获取",manual:"手动素材"};
    appendText(outer,"span",states[song.prep_state]||"排队中","status"+(song.prep_state==="unavailable"?" wait":""));
    if(song.prep_state==="unavailable"){
      const a=appendText(outer,"a","B站查看");a.href="https://www.bilibili.com/video/"+song.bvid+"/?p="+song.page;a.target="_blank";a.rel="noopener noreferrer";
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
          toast("媒体上传成功，可以排队播放"); await refresh();
        },upload);
      });
    }
    const lyrics=appendText(actions,"button","歌词","mini");lyrics.onclick=()=>editLyrics(song.id,song.title);
    if(song.status==="ready"){
      const render=appendText(actions,"button","制作 KTV","mini");render.onclick=()=>guarded(async()=>{
        const lead=Number($("lead-seconds").value);
        await api(`/api/admin/songs/${song.id}/render`,"POST",{lead});
        toast("字幕制作已开始，完成后会自动进入可播队列");await refresh();
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
    const status=song.status==="ready"?(song.prep_state==="done"?"KTV 字幕已完成":(song.prep_state==="aligning"?"原片可播 · 字幕制作中":(!song.lyric_selected?"原片可播 · 等待选歌词":"可播"))):(song.prep_state==="downloading"?"下载中":song.prep_state==="unavailable"?"视频暂不可用":"准备中");
    appendText(outer,"span",status,"status"+(song.status==="ready"?"":" wait"));
  }
  return outer;
}
function fillList(id, songs, mode="guest") {
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
  info.pages.forEach(p=>{const opt=document.createElement("option");opt.value=p.page;opt.textContent=`P${p.page} · ${p.part}`;$("page-select").append(opt);});
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
      appendText(heading,"span",candidate.source+" · "+candidate.text.split("\n").length+" 行");
      card.append(heading);
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
        toast("歌词已选定！后台会自动对齐并制作 KTV 字幕");
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
  const nickname=$("nickname").value.trim();
  if(nickname)localStorage.setItem("vc-ktv-nick",nickname);
  const result=await api("/api/songs","POST",{bvid,nickname,page:1});
  $("bvid").value="";
  $("preview").classList.add("hidden");
  preview=null;
  toast("已点歌！请选择对应的歌词版本；视频会同时准备");
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
  const nickname=$("nickname").value.trim();
  if(nickname)localStorage.setItem("vc-ktv-nick",nickname);
  const title=preview.title;
  const result=await api("/api/songs","POST",{bvid:preview.bvid,page:preview.page,nickname});
  toast("点歌成功，接下来请选择歌词");$("preview").classList.add("hidden");
  preview=null;await refresh();
  await openLyricPicker(result.id,title);
},$("submit-btn"));
function showTab(tab){
  const isAdmin=tab==="admin";
  $("guest-view").classList.toggle("hidden",isAdmin);
  $("admin-view").classList.toggle("hidden",!isAdmin);
  $("tab-guest").classList.toggle("selected",!isAdmin);
  $("tab-admin").classList.toggle("selected",isAdmin);
  if(isAdmin)refresh();
}
$("admin-advanced").addEventListener("toggle",()=>{
  if($("admin-advanced").open)refresh();
});
$("tab-guest").onclick=()=>showTab("guest");
$("tab-admin").onclick=()=>showTab("admin");
$("login-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/login","POST",{password:$("password").value});
  $("password").value="";admin=true;toast("管理员登录成功");await refresh();
},$("login-btn"));
$("password").onkeydown=(e)=>{if(e.key==="Enter")$("login-btn").click();};
$("logout-btn").onclick=()=>guarded(async()=>{
  await api("/api/admin/logout","POST");admin=false;currentMediaId=null;
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
  $("playing-title").textContent=`正在播放：${active.title} · ${active.nickname}`;
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
    const btn=appendText(results,"button",`使用候选 ${i+1} · ${c.text.split("\n").length} 行 · 需确认版本`,"mini");
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
async function refresh(){
  if(refreshing)return;
  refreshing=true;
  try{
    const state=await api("/api/state");
    $("queue-count").textContent=`${state.queue.length} 首可播`;
    fillList("queue-ready",state.queue);
    fillList("queue-waiting",state.waiting);
    const current=$("current-guest");current.replaceChildren();
    appendText(current,"span","正在播放","current-label");
    if(state.current){
      appendText(current,"strong",state.current.title);
      appendText(current,"small",`点歌者：${state.current.nickname}`);
    }else appendText(current,"p","当前空闲，等待管理员开始播放");
    const tabVisible=!$("admin-view").classList.contains("hidden");
    if(tabVisible){
      let adm;
      try{adm=await api("/api/admin/state");admin=true;}
      catch(e){if(e.message.includes("管理员"))admin=false;else throw e;}
      $("login-box").classList.toggle("hidden",admin);
      $("admin-panel").classList.toggle("hidden",!admin);
      if(admin){
        const pending=[...adm.waiting,...adm.queue];
        fillList("manage-status",pending,"status");
        if($("admin-advanced").open)fillList("manage-list",pending,"admin");
        const stats=$("stats");stats.replaceChildren();
        if(!adm.guests.length)appendText(stats,"div","暂无演唱记录","empty");
        adm.guests.forEach(u=>{
          const c=document.createElement("div");c.className="stat";
          appendText(c,"strong",u.name);
          appendText(c,"small",`${u.plays} 首 · ${duration(u.seconds)}`);stats.append(c);
        });
        if(adm.active){
          if(currentMediaId!==adm.active.id)await assignPlayer(adm.active,false);
          $("playing-title").textContent=`正在播放：${adm.active.title} · ${adm.active.nickname}`;
        }else if(currentMediaId!==null){
          currentMediaId=null;player.pause();player.removeAttribute("src");player.load();
          $("player-empty").classList.remove("hidden");
          $("playing-title").textContent="当前没有正在播放的歌曲";
        }
      }
    }
  }catch(e){error(e);}finally{refreshing=false;}
}
refresh();
setInterval(()=>{if(document.visibilityState==="visible")refresh();},3500);
