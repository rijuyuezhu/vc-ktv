import {$, api, guarded, toast} from "./ui-common.js";
// Event playback, queue and media configuration forms.
export function setupAdminSettings({refresh, invalidateQueueState}) {
  let settingsEventId=null;
  async function refreshAdminSettings(activeId){
  if(!activeId){settingsEventId=null;return;}
  const settings=await api("/api/admin/settings");
  if(settingsEventId!==activeId){
    settingsEventId=activeId;
    $("queue-policy").value=settings.queue_policy;
    $("subtitle-style").value=settings.subtitle_style;
    $("video-quality").value=String(settings.video_quality);
    $("allow-unsubtitled").checked=settings.allow_unsubtitled;
    $("max-bandwidth-mbps").value=String(settings.max_bandwidth_mbps);
    $("pause-orders").checked=settings.orders_paused;
    $("tv-html-overlay").checked=settings.tv_html_overlay;
    $("force-h264-originals").checked=settings.force_h264_originals;
  }else if(document.activeElement!==$("queue-policy")&&
           document.activeElement!==$("allow-unsubtitled")&&
           document.activeElement!==$("subtitle-style")&&
           document.activeElement!==$("video-quality")&&
           document.activeElement!==$("max-bandwidth-mbps")&&
           document.activeElement!==$("pause-orders")&&
           document.activeElement!==$("tv-html-overlay")&&
           document.activeElement!==$("force-h264-originals")&&
           document.activeElement!==$("settings-save-btn")){
    $("queue-policy").value=settings.queue_policy;
    $("subtitle-style").value=settings.subtitle_style;
    $("video-quality").value=String(settings.video_quality);
    $("allow-unsubtitled").checked=settings.allow_unsubtitled;
    $("max-bandwidth-mbps").value=String(settings.max_bandwidth_mbps);
    $("pause-orders").checked=settings.orders_paused;
    $("tv-html-overlay").checked=settings.tv_html_overlay;
    $("force-h264-originals").checked=settings.force_h264_originals;
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
    tv_html_overlay:$("tv-html-overlay").checked,
    force_h264_originals:$("force-h264-originals").checked
  });
  toast("已保存");await refresh();
},$("settings-save-btn"));
$("queue-settings-save").onclick=()=>guarded(async()=>{
  await api("/api/admin/settings","PUT",{
    queue_policy:$("queue-policy").value,
    allow_unsubtitled:$("allow-unsubtitled").checked,
    orders_paused:$("pause-orders").checked
  });
  invalidateQueueState();
  toast("已保存");await refresh();
},$("queue-settings-save"));
  return {refreshAdminSettings};
}
