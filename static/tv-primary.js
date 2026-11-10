import {$, api, toast, guarded} from "./ui-common.js";
// Primary-screen lifecycle and status polling for the administrator page.
// Backend owns the actual single-primary and administrator permission rules.
export function setupTVPrimary({isAdminPath, getPlayback, updateRemoteControls}) {
let tvPrimaryState=null;
let tvRemoteState=null;
let remoteStatusInFlight=null;
async function loadTVRemoteStatus(){
  if(remoteStatusInFlight)return remoteStatusInFlight;
  remoteStatusInFlight=api("/api/admin/tv/remote");
  try{return await remoteStatusInFlight;}
  finally{remoteStatusInFlight=null;}
}
async function pollTVRemoteStatus(){
  const {admin,playbackMode}=getPlayback();
  if(!isAdminPath()||!admin||playbackMode!=="tv"||!tvPrimaryState?.active||
     document.visibilityState!=="visible"||
     !document.querySelector(".playback-remote")?.getClientRects().length)return;
  try{
    const status=await loadTVRemoteStatus();
    if(getPlayback().playbackMode==="tv"&&tvPrimaryState?.active){
      tvRemoteState=status;
      updateRemoteControls();
    }
  }catch{
    // Ordinary admin refresh already handles connection errors; polling must
    // never create a toast or disrupt user input.
  }
}
async function refreshTVPrimary(){
  const tv=await api("/api/admin/tv");
  const {playbackMode}=getPlayback();
  tvPrimaryState=tv;
  const controls=$("tv-primary-management");
  controls.classList.toggle("hidden",playbackMode!=="tv");
  tvRemoteState=playbackMode==="tv"&&tv.active?
    await loadTVRemoteStatus():null;
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
  return {
    refreshTVPrimary,
    pollTVRemoteStatus,
    state:()=>({tvPrimaryState,tvRemoteState}),
  };
}
