import {$, api, guarded, toast, error, appendText, duration} from "./ui-common.js";
// Guest Bilibili preview, NetEase lookup and song request actions.
export function setupSongRequests({refresh, openLyricPicker}) {
  let preview = null;

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
    neteaseSearchVersion++;
    neteaseLastQuery="";
    neteasePage=0;
    neteaseCanLoadMore=false;
    $("netease-song-query").value="";
    $("netease-search-results").replaceChildren();
    $("netease-search-message").textContent="已点歌，可以继续搜索下一首。";
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
  $("bvid").value="";
  preview=null;await refresh();
  await openLyricPicker(result.id,title,result.lyric_query);
},$("submit-btn"));
}
