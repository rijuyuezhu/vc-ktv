// Shared browser UI primitives; feature state stays in its own module.
const $ = (id) => document.getElementById(id);
export { $ };
let toastTimer = null;

export function toast(message, error = false) {
  clearTimeout(toastTimer);
  const t = $("toast");
  t.textContent = message;
  t.className = "show" + (error ? " error" : "");
  t.setAttribute("role", error ? "alert" : "status");
  t.setAttribute("aria-live", error ? "assertive" : "polite");
  toastTimer = setTimeout(() => {
    t.className = "";
  }, 4800);
}
export async function api(url, method = "GET", body = null, signal = null) {
  let opts = { method, credentials: "same-origin", cache: "no-store", signal };
  if (body !== null) {
    if (body instanceof FormData) opts.body = body;
    else {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
  }
  const res = await fetch(url, opts);
  let data;
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}
export function error(e) {
  toast(e.message || "操作失败", true);
}
export async function guarded(action, button = null) {
  try {
    if (button) button.disabled = true;
    return await action();
  } catch (e) {
    error(e);
  } finally {
    if (button) button.disabled = false;
  }
}
export function duration(n) {
  n = Math.round(Number(n) || 0);
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
}
export function appendText(parent, tag, text, cls = "") {
  const el = document.createElement(tag);
  el.textContent = text;
  if (cls) el.className = cls;
  parent.append(el);
  return el;
}

// The same event row is used in public playlists and administrator archives.
export function eventRow(parent, title, metadata, onOpen) {
  const row = appendText(parent, "button", "", "song-item archive-entry");
  row.type = "button";
  appendText(row, "strong", title);
  appendText(row, "small", metadata);
  appendText(row, "span", "›", "song-chevron").setAttribute("aria-hidden", "true");
  row.onclick = onOpen;
  return row;
}
export function actionButton(parent, label, action, style = "neutral") {
  const button = appendText(parent, "button", label, "btn " + style);
  button.type = "button";
  button.onclick = () => guarded(() => action(button), button);
  return button;
}
