import { $, api, guarded, toast } from "./ui-common.js";
// Event playback, queue and media configuration forms.
export function setupAdminSettings({ refresh, invalidateQueueState }) {
  let settingsEventId = null;
  const dirty = new Set();
  const fields = {
    "queue-policy": "queue_policy",
    "subtitle-style": "subtitle_style",
    "video-quality": "video_quality",
    "allow-unsubtitled": "allow_unsubtitled",
    "max-bandwidth-mbps": "max_bandwidth_mbps",
    "pause-orders": "orders_paused",
    "tv-html-overlay": "tv_html_overlay",
    "original-codec-mode": "original_codec_mode"
  };
  const readValue = (id) => {
    const input = $(id);
    return input.type === "checkbox" ? input.checked : input.value;
  };
  for (const id of Object.keys(fields)) {
    const input = $(id);
    const changed = () => dirty.add(id);
    input.addEventListener("input", changed);
    input.addEventListener("change", changed);
  }
  function applySettings(settings, reset) {
    for (const [id, name] of Object.entries(fields)) {
      if (!reset && dirty.has(id)) continue;
      const input = $(id);
      if (input.type === "checkbox") input.checked = Boolean(settings[name]);
      else input.value = String(settings[name]);
    }
  }
  function savedValues(ids) {
    return Object.fromEntries(ids.map((id) => [id, readValue(id)]));
  }
  function markSaved(snapshot) {
    for (const [id, value] of Object.entries(snapshot)) {
      if (readValue(id) === value) dirty.delete(id);
    }
  }
  async function refreshAdminSettings(activeId) {
    if (!activeId) {
      settingsEventId = null;
      dirty.clear();
      return;
    }
    const settings = await api("/api/admin/settings");
    const reset = settingsEventId !== activeId;
    if (reset) dirty.clear();
    applySettings(settings, reset);
    settingsEventId = activeId;
    updateStylePreview();
  }
  function updateStylePreview() {
    $("style-preview").className = "style-preview " + $("subtitle-style").value;
  }
  $("subtitle-style").onchange = updateStylePreview;
  $("settings-save-btn").onclick = () =>
    guarded(async () => {
      const snapshot = savedValues([
        "subtitle-style",
        "video-quality",
        "max-bandwidth-mbps",
        "tv-html-overlay",
        "original-codec-mode"
      ]);
      await api("/api/admin/settings", "PUT", {
        subtitle_style: $("subtitle-style").value,
        video_quality: Number($("video-quality").value),
        max_bandwidth_mbps: Number($("max-bandwidth-mbps").value),
        tv_html_overlay: $("tv-html-overlay").checked,
        original_codec_mode: $("original-codec-mode").value
      });
      markSaved(snapshot);
      toast("已保存");
      await refresh();
    }, $("settings-save-btn"));
  $("queue-settings-save").onclick = () =>
    guarded(async () => {
      const snapshot = savedValues(["queue-policy", "allow-unsubtitled", "pause-orders"]);
      await api("/api/admin/settings", "PUT", {
        queue_policy: $("queue-policy").value,
        allow_unsubtitled: $("allow-unsubtitled").checked,
        orders_paused: $("pause-orders").checked
      });
      markSaved(snapshot);
      invalidateQueueState();
      toast("已保存");
      await refresh();
    }, $("queue-settings-save"));
  return { refreshAdminSettings };
}
