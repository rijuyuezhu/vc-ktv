import { $, api, guarded, toast } from "./ui-common.js";
// NetEase login connection UI; only this module owns its QR polling timers.
export function setupNeteaseConnection() {
  let neteaseLoginPolling = null;
  let neteaseLoginChecking = false;
  function stopNeteaseLoginPolling() {
    if (neteaseLoginPolling !== null) clearInterval(neteaseLoginPolling);
    neteaseLoginPolling = null;
  }
  function displayNeteaseLoginStatus(state) {
    const connected = Boolean(state.connected);
    const pending = Boolean(state.pending) && !connected;
    $("netease-login-start").classList.toggle("hidden", connected || pending);
    $("netease-login-disconnect").classList.toggle("hidden", !connected && !pending);
    $("netease-login-check").classList.add("hidden");
    if (!pending) $("netease-login-qr-box").classList.add("hidden");
    $("netease-login-status").textContent = connected
      ? "✓ 已保存网易云登录态（服务重启后可继续使用）"
      : state.phase === "verification_required"
        ? "网易云要求安全验证（8821）。本次扫码已暂停；可在官方网页正常登录，再使用下方网页登录态连接。"
        : pending
          ? state.phase === "confirm"
            ? "请在网易云音乐 App 中确认登录"
            : "等待扫码确认……"
          : state.phase === "expired"
            ? "二维码已过期，请重新扫码"
            : "尚未连接网易云";
    if (connected || !pending || state.phase === "verification_required") stopNeteaseLoginPolling();
  }
  async function refreshNeteaseLogin() {
    const state = await api("/api/admin/netease/status");
    displayNeteaseLoginStatus(state);
    if (state.pending && state.phase !== "verification_required") {
      if ($("netease-login-qr-image").getAttribute("src")) {
        stopNeteaseLoginPolling();
        neteaseLoginPolling = setInterval(checkNeteaseLogin, 2500);
      } else {
        // Another browser initiated the QR; cancel and start a new one here.
        $("netease-login-status").textContent = "其他页面正在扫码，可取消后重新连接";
      }
    }
  }
  async function checkNeteaseLogin() {
    if (neteaseLoginChecking) return;
    neteaseLoginChecking = true;
    try {
      const state = await api("/api/admin/netease/check", "POST");
      displayNeteaseLoginStatus(state);
      if (state.connected) toast("✓ 网易云音乐已连接");
      if (state.phase === "verification_required") $("netease-cookie-import-panel").open = true;
    } catch (e) {
      stopNeteaseLoginPolling();
      $("netease-login-status").textContent = e.message || "扫码检查失败，请稍后重试";
      $("netease-login-check").classList.remove("hidden");
    } finally {
      neteaseLoginChecking = false;
    }
  }
  $("netease-cookie-import-form").addEventListener("submit", (event) => {
    event.preventDefault();
    guarded(async () => {
      const field = $("netease-cookie-import-value");
      const value = field.value.trim();
      if (!value) throw new Error("请提供 MUSIC_U");
      try {
        const state = await api("/api/admin/netease/import", "POST", { music_u: value });
        stopNeteaseLoginPolling();
        $("netease-login-qr-image").removeAttribute("src");
        displayNeteaseLoginStatus(state);
        $("netease-cookie-import-panel").open = false;
        toast("✓ 网易云网页登录态已验证并保存");
      } finally {
        field.value = "";
      }
    }, $("netease-cookie-import-submit"));
  });
  $("netease-login-check").onclick = () => guarded(checkNeteaseLogin, $("netease-login-check"));
  $("netease-login-start").onclick = () =>
    guarded(async () => {
      const state = await api("/api/admin/netease/qr", "POST");
      displayNeteaseLoginStatus(state);
      if (state.qr_image) {
        $("netease-login-qr-image").src = state.qr_image;
        $("netease-login-qr-box").classList.remove("hidden");
        stopNeteaseLoginPolling();
        neteaseLoginPolling = setInterval(checkNeteaseLogin, 2500);
      }
    }, $("netease-login-start"));
  $("netease-login-disconnect").onclick = () =>
    guarded(async () => {
      stopNeteaseLoginPolling();
      const wasConnected =
        !$("netease-login-disconnect").classList.contains("hidden") &&
        $("netease-login-qr-box").classList.contains("hidden");
      const state = await api("/api/admin/netease/disconnect", "POST");
      $("netease-login-qr-image").removeAttribute("src");
      displayNeteaseLoginStatus(state);
      toast(wasConnected ? "已断开网易云连接" : "已取消扫码");
    }, $("netease-login-disconnect"));
  return { stopNeteaseLoginPolling, refreshNeteaseLogin };
}
