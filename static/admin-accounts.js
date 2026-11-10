import { $, api, guarded, toast, appendText } from "./ui-common.js";
// Global administrator's event-specific admin-account controls.
export function setupAdminAccounts() {
  async function refreshAdminAccounts() {
    const target = $("admin-account-list");
    const data = await api("/api/admin/accounts");
    target.replaceChildren();
    if (!data.accounts.length) {
      appendText(target, "p", "本场尚未创建管理员", "muted");
      return;
    }
    data.accounts.forEach((account) => {
      const item = appendText(target, "details", "", "account-entry");
      appendText(item, "summary", account.username);
      const actions = appendText(item, "div", "", "account-entry-actions");
      const password = document.createElement("input");
      password.type = "password";
      password.autocomplete = "new-password";
      password.minLength = 6;
      password.maxLength = 72;
      password.placeholder = "新密码";
      password.setAttribute("aria-label", "重置 " + account.username + " 的密码");
      actions.append(password);
      const reset = appendText(actions, "button", "重置密码", "btn neutral");
      reset.type = "button";
      reset.onclick = () =>
        guarded(async () => {
          if (password.value.length < 6) throw new Error("密码至少 6 个字符");
          await api("/api/admin/accounts/" + account.id + "/password", "PUT", {
            password: password.value
          });
          password.value = "";
          toast("密码已重置");
        }, reset);
      const revoke = appendText(actions, "button", "撤销账号", "btn subtle");
      revoke.type = "button";
      revoke.onclick = () =>
        guarded(async () => {
          if (!confirm("撤销管理员「" + account.username + "」？")) return;
          await api("/api/admin/accounts/" + account.id, "DELETE");
          toast("账号已撤销");
          await refreshAdminAccounts();
        }, revoke);
    });
  }
  $("admin-accounts-panel").addEventListener("toggle", () => {
    if ($("admin-accounts-panel").open && !$("admin-accounts-panel").classList.contains("hidden"))
      guarded(refreshAdminAccounts);
  });
  $("admin-account-create").onclick = () =>
    guarded(async () => {
      const username = $("admin-account-name").value.trim();
      const password = $("admin-account-password").value;
      if (!username || password.length < 6) throw new Error("请输入用户名和至少 6 位的密码");
      await api("/api/admin/accounts", "POST", { username, password });
      $("admin-account-name").value = "";
      $("admin-account-password").value = "";
      toast("本场管理员已创建");
      await refreshAdminAccounts();
    }, $("admin-account-create"));
  return { refreshAdminAccounts };
}
