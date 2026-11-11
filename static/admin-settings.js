import { $, api, guarded, toast } from "./ui-common.js";
// Event playback, queue and media configuration forms.
export function setupAdminSettings({ refresh, invalidateQueueState }) {
  let settingsEventId = null;
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
  function applySettings(settings) {
    for (const [id, name] of Object.entries(fields)) {
      const input = $(id);
      if (input.type === "checkbox") input.checked = Boolean(settings[name]);
      else input.value = String(settings[name]);
    }
  }
  async function refreshAdminSettings(activeId) {
    if (!activeId) {
      settingsEventId = null;
      return;
    }
    const settings = await api("/api/admin/settings");
    const editing = ["settings-save-btn", ...Object.keys(fields)].includes(
      document.activeElement?.id
    );
    if (settingsEventId !== activeId || !editing) applySettings(settings);
    settingsEventId = activeId;
    updateStylePreview();
  }
  function updateStylePreview() {
    $("style-preview").className = "style-preview " + $("subtitle-style").value;
  }
  $("subtitle-style").onchange = updateStylePreview;
  $("settings-save-btn").onclick = () =>
    guarded(async () => {
      await api("/api/admin/settings", "PUT", {
        subtitle_style: $("subtitle-style").value,
        video_quality: Number($("video-quality").value),
        max_bandwidth_mbps: Number($("max-bandwidth-mbps").value),
        tv_html_overlay: $("tv-html-overlay").checked,
        original_codec_mode: $("original-codec-mode").value
      });
      toast("已保存");
      await refresh();
    }, $("settings-save-btn"));
  $("queue-settings-save").onclick = () =>
    guarded(async () => {
      await api("/api/admin/settings", "PUT", {
        queue_policy: $("queue-policy").value,
        allow_unsubtitled: $("allow-unsubtitled").checked,
        orders_paused: $("pause-orders").checked
      });
      invalidateQueueState();
      toast("已保存");
      await refresh();
    }, $("queue-settings-save"));
  return { refreshAdminSettings };
}
