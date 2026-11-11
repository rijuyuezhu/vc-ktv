import { setupAdminSettings } from "./admin-settings.js";
import { setupAdminAccounts } from "./admin-accounts.js";
import { setupNeteaseConnection } from "./admin-netease.js";
import { $, api, toast, guarded, eventRow, emptyState } from "./ui-common.js";

// Administrator event lifecycle, permissions and archive list.
export function setupAdminEvents({
  refresh,
  currentRoute,
  onEventChanged,
  updateAccount,
  onAdminRole = () => {},
  openArchivedEvent,
  invalidateQueueState
}) {
  let archiveListKey = null;
  let adminAccountsEventId = null;
  let neteaseLoginPageActive = false;
  let adminCurrentEvent = null;

  function leaveNeteasePage() {
    stopNeteaseLoginPolling();
    neteaseLoginPageActive = false;
  }

  function renderEventArchive(data) {
    const entries = data.role === "global" ? data.events.filter((e) => e.status === "closed") : [];
    const key = JSON.stringify([data.role, entries]);
    if (key === archiveListKey) return;
    archiveListKey = key;
    const target = $("event-history");
    target.replaceChildren();
    if (!entries.length) emptyState(target, "暂无历史场次");
    for (const event of entries)
      eventRow(target, event.name, event.public ? "公开" : "私有", () =>
        openArchivedEvent(event.id)
      );
  }

  const { stopNeteaseLoginPolling, refreshNeteaseLogin } = setupNeteaseConnection();
  const { refreshAdminAccounts } = setupAdminAccounts();
  const { refreshAdminSettings } = setupAdminSettings({
    refresh,
    invalidateQueueState
  });
  async function refreshEvents() {
    const data = await api("/api/admin/events");
    onAdminRole(data.role);
    if (adminCurrentEvent !== data.active) {
      adminCurrentEvent = data.active;
      onEventChanged();
    }
    const active = data.events.find((e) => e.id === data.active);
    $("event-status").textContent = active ? "♫ " + active.name : "尚未创建场次";
    $("event-join-code").textContent = active && !active.legacy ? "加入码  " + active.code : "";
    $("event-create-fields").classList.toggle("hidden", !!active || data.role !== "global");
    $("event-close-btn").classList.toggle("hidden", !active);
    $("event-settings-panel").classList.toggle("hidden", !active);
    $("event-queue-settings-panel").classList.toggle("hidden", !active);
    const canManageAccounts = Boolean(active && data.role === "global");
    for (const route of ["settings-accounts", "settings-netease"]) {
      document
        .querySelector('a[href="#' + route + '"]')
        ?.classList.toggle("hidden", data.role !== "global");
    }
    if (
      data.role !== "global" &&
      ["settings-accounts", "settings-netease"].includes(currentRoute())
    )
      location.hash = "settings";
    $("admin-accounts-panel").classList.toggle("hidden", !canManageAccounts);
    $("netease-admin-panel").classList.toggle("hidden", data.role !== "global");
    if (data.role !== "global") {
      leaveNeteasePage();
    } else if (currentRoute() === "settings-netease" && !neteaseLoginPageActive) {
      neteaseLoginPageActive = true;
      await refreshNeteaseLogin();
    }
    if (adminAccountsEventId !== (canManageAccounts ? active.id : null)) {
      adminAccountsEventId = canManageAccounts ? active.id : null;
      $("admin-account-list").replaceChildren();
      if (canManageAccounts && $("admin-accounts-panel").open) await refreshAdminAccounts();
    }
    $("admin-role").textContent =
      data.role === "global" ? "全局管理员" : "本场管理员：" + (data.username || "");
    updateAccount(
      true,
      data.role === "global" ? "管理员" : data.username || "管理员",
      data.role === "global" ? "全局管理员" : "本场管理员"
    );

    renderEventArchive(data);
    await refreshAdminSettings(active?.id || null);
  }
  $("event-create-btn").onclick = () =>
    guarded(async () => {
      const name = $("event-name").value.trim();
      const code = $("event-code").value.trim();
      if (!name) throw new Error("请填写场次名称");
      await api("/api/admin/events", "POST", { name, code: code || null });
      $("event-name").value = "";
      $("event-code").value = "";
      toast("新场次已创建");
      await refresh();
    }, $("event-create-btn"));
  $("event-close-btn").onclick = () =>
    guarded(async () => {
      if (!confirm("结束本场？正在播放的歌曲将中断，未唱歌曲会保留记录。")) return;
      await api("/api/admin/events/close", "POST");
      toast("✓ 已归档");
      await refresh();
    }, $("event-close-btn"));

  return { refreshEvents, leaveNeteasePage };
}
