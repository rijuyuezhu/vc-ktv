import {setupTVPrimary} from "./tv-primary.js";
import {setupRemoteControls} from "./remote-controls.js";
import {$, api, toast, guarded} from "./ui-common.js";

// One owner for administrator TV, browser and manual playback state.
export function setupAdminPlayback({refresh,isAdminPath,getAdmin,addSourceTag,invalidateAdminCache}){
  let currentMediaId=null;
  let manualMode=false;
  let playbackMode="tv";
  let currentAdminSong=null;
  let nextAdminSong=null;
  const player=$("player");

  function songSource(song){
    return song.source_type==="netease"?
      {label:"🎵 网易云",copyLabel:"网易云链接",ref:song.source_id?
        "https://music.163.com/#/song?id="+encodeURIComponent(song.source_id):""}:
      {label:"📺 B站",copyLabel:"BV 号",ref:song.bvid||""};
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
    }catch{
      prompt("复制"+source.copyLabel,source.ref);
    }
  }
  const primaryTV=setupTVPrimary({
    isAdminPath,
    getPlayback:()=>({admin:getAdmin(),playbackMode}),
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
      invalidateAdminCache();updatePlaybackUI();await refresh();
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

  async function syncFromAdminState(adm,changed){
    playbackMode=adm.playback_mode||"tv";
    manualMode=playbackMode==="manual";
    currentAdminSong=adm.active||null;
    nextAdminSong=adm.queue?.[0]||null;
    await refreshTVPrimary();
    $("playback-mode").disabled=Boolean(adm.active);
    updatePlaybackUI();
    if(changed){
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
  }

  async function prepareLogout(){
    if(playbackMode==="browser"&&currentAdminSong){
      if(!confirm("当前歌曲正在播放，退出将跳过它。确定退出？"))return false;
      await finishActive("skip",false);
    }
    return true;
  }

  function resetPlayer(){
    currentMediaId=null;
    player.pause();player.removeAttribute("src");player.load();
  }

  return {syncFromAdminState,pollTVRemoteStatus,updateRemoteControls,
    updatePlaybackUI,prepareLogout,resetPlayer};
}
