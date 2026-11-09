import {setupAdminSettings} from "./admin-settings.js";
import {setupAdminAccounts} from "./admin-accounts.js";
import {setupNeteaseConnection} from "./admin-netease.js";
import {$, api, toast, guarded, appendText} from "./ui-common.js";

// Administrator event lifecycle, permissions and archive list.
export function setupAdminEvents({
  refresh, currentRoute, onEventChanged, updateAccount,
  addSourceTag, songState, invalidateQueueState
}){
  let archiveListKey=null;
  let adminAccountsEventId=null;
  let neteaseLoginPageActive=false;
  let adminCurrentEvent=null;

  function leaveNeteasePage(){
    stopNeteaseLoginPolling();
    neteaseLoginPageActive=false;
  }

  function renderEventArchive(data){
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
  }

  const {stopNeteaseLoginPolling,refreshNeteaseLogin} = setupNeteaseConnection();
  const {refreshAdminAccounts} = setupAdminAccounts();
  const {refreshAdminSettings} = setupAdminSettings({
    refresh,
    invalidateQueueState
  });
  async function refreshEvents(){
    const data=await api("/api/admin/events");
    if(adminCurrentEvent!==data.active){
      adminCurrentEvent=data.active;
      onEventChanged();
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
      leaveNeteasePage();
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

    renderEventArchive(data);
    await refreshAdminSettings(active?.id||null);
  }
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

  return {refreshEvents,leaveNeteasePage};
}
