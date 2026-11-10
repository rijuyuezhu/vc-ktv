import { $, api, error, toast } from "./ui-common.js";
// Administrator volume, song transport and TV remote presentation.
// Playback lifecycle is owned by admin-playback.js.
export function setupRemoteControls({ refresh, finishActive, getState, player }) {
  function clock(seconds) {
    return Number.isFinite(seconds)
      ? Math.floor(seconds / 60) + ":" + String(Math.floor(seconds % 60)).padStart(2, "0")
      : "0:00";
  }
  function updateRemoteControls() {
    const {
      playbackMode,
      tvRemoteState,
      tvPrimaryState,
      currentAdminSong,
      nextAdminSong,
      currentMediaId
    } = getState();
    const isTV = playbackMode === "tv";
    const isBrowser = playbackMode === "browser";
    const status = tvRemoteState?.report || {};
    const online = Boolean(isTV && tvRemoteState?.online);
    const canTV = Boolean(online && tvRemoteState?.can_manage && !tvRemoteState?.pending);
    const songId = currentAdminSong?.id || null;
    const tvPlaying = online && status.active_id === songId && Boolean(songId);
    const playable = Boolean(
      songId && (isBrowser ? currentMediaId === songId : isTV ? tvPlaying : false)
    );
    const remoteReady = isTV ? canTV && playable : playable;
    for (const name of ["remote-pause", "remote-restart", "remote-skip", "remote-replay"]) {
      const button = $(name);
      button.disabled = isTV ? !remoteReady : !isBrowser || !playable;
    }
    if (playbackMode === "manual" && songId) $("remote-replay").disabled = false;
    const paused = isTV ? Boolean(status.paused) : player.paused;
    const pause = $("remote-pause");
    pause.title = paused ? "继续" : "暂停";
    pause.setAttribute("aria-label", pause.title);
    pause.querySelector("img").src = "/icons/" + (paused ? "play" : "pause") + ".svg";
    $("remote-progress").textContent = isTV
      ? clock(status.position) + " / " + clock(status.duration)
      : clock(player.currentTime) + " / " + clock(player.duration);
    $("remote-progress").classList.toggle("hidden", playbackMode === "manual");
    const volume = $("remote-volume");
    const hasVolume = isTV ? canTV : isBrowser;
    volume.disabled = !hasVolume;
    volume.closest(".remote-range").classList.toggle("hidden", playbackMode === "manual");
    if (document.activeElement !== volume)
      volume.value = String(isTV ? (status.volume ?? 100) : Math.round(player.volume * 100));
    $("tv-remote-extra").classList.toggle("hidden", !isTV);
    for (const button of document.querySelectorAll("[data-admin-offset]"))
      button.disabled = !canTV || !tvPlaying || !status.overlay;
    const offs = Number(status.offset || 0);
    $("remote-offset-value").textContent = (offs >= 0 ? "+" : "") + (offs / 10).toFixed(1) + "s";
    const opacity = $("remote-opacity");
    opacity.disabled = !canTV;
    if (document.activeElement !== opacity) opacity.value = String(status.opacity ?? 15);
    const smooth = $("remote-smooth");
    smooth.disabled = !canTV;
    smooth.checked = Boolean(status.smooth);
    const indicator = $("tv-online");
    indicator.textContent = online
      ? "主屏在线"
      : tvPrimaryState?.active
        ? "主屏离线"
        : "主屏未连接";
    indicator.classList.toggle("online", online);
    const displayTitle = $("tv-playing");
    displayTitle.textContent = currentAdminSong?.title || "等待播放";
    displayTitle.href = currentAdminSong ? "#song-" + currentAdminSong.id : "#";
    displayTitle.tabIndex = currentAdminSong ? 0 : -1;
    $("tv-coming").textContent = nextAdminSong ? "下一首：" + nextAdminSong.title : "";
    if (status.error && isTV) $("remote-error").textContent = status.error;
    else $("remote-error").textContent = "";
  }
  async function sendRemote(action, value = null) {
    const { playbackMode, currentAdminSong, tvRemoteState } = getState();
    if (playbackMode !== "tv") throw Error("当前不是电视模式");
    const hasSong = ["pause", "resume", "restart", "skip", "replay_next"].includes(action);
    await api("/api/admin/tv/remote", "POST", {
      action,
      song_id: hasSong ? currentAdminSong?.id : null,
      value
    });
    if (tvRemoteState) tvRemoteState.pending = true;
    updateRemoteControls();
  }
  async function remoteAction(button, action) {
    button.disabled = true;
    try {
      await action();
    } catch (e) {
      error(e);
    } finally {
      updateRemoteControls();
    }
  }
  $("remote-pause").onclick = () =>
    remoteAction($("remote-pause"), async () => {
      const { playbackMode, tvRemoteState } = getState();
      if (playbackMode === "tv")
        await sendRemote(tvRemoteState?.report?.paused ? "resume" : "pause");
      else if (playbackMode === "browser") {
        if (player.paused) await player.play();
        else player.pause();
      }
    });
  $("remote-restart").onclick = () =>
    remoteAction($("remote-restart"), async () => {
      const { playbackMode } = getState();
      if (playbackMode === "tv") await sendRemote("restart");
      else if (playbackMode === "browser") {
        player.currentTime = 0;
        await player.play();
      }
    });
  $("remote-skip").onclick = () =>
    remoteAction($("remote-skip"), async () => {
      const { playbackMode } = getState();
      if (playbackMode === "tv") await sendRemote("skip");
      else if (playbackMode === "browser") await finishActive("skip", true);
    });
  $("remote-replay").onclick = () =>
    remoteAction($("remote-replay"), async () => {
      const { playbackMode, currentAdminSong } = getState();
      if (playbackMode === "tv") await sendRemote("replay_next");
      else if (currentAdminSong) {
        await api("/api/admin/songs/" + currentAdminSong.id + "/replay-next", "POST");
        toast("已安排下一首重唱");
        await refresh();
      }
    });
  $("remote-volume").oninput = () => {
    if (getState().playbackMode === "browser")
      player.volume = Number($("remote-volume").value) / 100;
  };
  $("remote-volume").onchange = () =>
    remoteAction($("remote-volume"), async () => {
      if (getState().playbackMode === "tv")
        await sendRemote("volume", Number($("remote-volume").value));
    });
  for (const button of document.querySelectorAll("[data-admin-offset]")) {
    button.onclick = () =>
      remoteAction(button, async () => {
        const current = Number(getState().tvRemoteState?.report?.offset || 0);
        await sendRemote(
          "offset",
          Math.max(-600, Math.min(600, current + Number(button.dataset.adminOffset)))
        );
      });
  }
  $("remote-opacity").onchange = () =>
    remoteAction($("remote-opacity"), () =>
      sendRemote("opacity", Number($("remote-opacity").value))
    );
  $("remote-smooth").onchange = () =>
    remoteAction($("remote-smooth"), () => sendRemote("smooth", $("remote-smooth").checked));
  player.addEventListener("timeupdate", () => {
    if (getState().playbackMode === "browser") updateRemoteControls();
  });
  player.addEventListener("play", updateRemoteControls);
  player.addEventListener("pause", updateRemoteControls);
  return { updateRemoteControls };
}
