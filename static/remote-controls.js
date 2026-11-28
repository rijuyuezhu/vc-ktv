import { $, api, error, toast } from "./ui-common.js";
import { clock, offsetLabel, adjustOffset, createOffsetButtons } from "./remote-timing.js";
// Administrator volume, song transport and TV remote presentation.
// Playback lifecycle is owned by admin-playback.js.
export function setupRemoteControls({ refresh, finishActive, getState, player }) {
  let sending = false;
  let optimisticPaused = null;
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
    const pending = Boolean(isTV && (sending || tvRemoteState?.pending));
    if (!pending) optimisticPaused = null;
    document.querySelector(".playback-remote").classList.toggle("remote-pending", pending);
    $("remote-command-status").textContent = pending
      ? sending
        ? "正在发送指令…"
        : "等待主屏确认…"
      : "";
    const canTV = Boolean(
      online && tvRemoteState?.can_manage && (!pending || (tvRemoteState?.can_override && !sending))
    );
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
    const paused = isTV
      ? pending && optimisticPaused !== null
        ? optimisticPaused
        : Boolean(status.paused)
      : player.paused;
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
    for (const button of offsetButtons) button.disabled = !canTV || !tvPlaying || !status.overlay;
    const offs = Number(status.offset || 0);
    $("remote-offset-value").textContent = offsetLabel(offs);
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
    const audioMode = $("remote-audio-mode");
    const backingAvailable = Boolean(status.backing_ready);
    const activeBacking = status.audio_mode === "backing";
    const showBacking = isTV && (backingAvailable || activeBacking);
    audioMode.classList.toggle("hidden", !showBacking);
    audioMode.title = activeBacking ? "切回原唱" : "切换伴奏";
    $("remote-audio-mode-icon").src =
      "/icons/" + (activeBacking ? "audio-original" : "audio-backing") + ".svg";
    audioMode.setAttribute("aria-label", audioMode.title);
    audioMode.setAttribute("aria-pressed", String(activeBacking));
    audioMode.disabled = !canTV || !tvPlaying || (!backingAvailable && !activeBacking);
    audioMode.closest(".remote-buttons").classList.toggle("has-backing", showBacking);
    if (status.error && isTV) $("remote-error").textContent = status.error;
    else $("remote-error").textContent = "";
  }
  async function sendRemote(action, value = null) {
    const { playbackMode, currentAdminSong, tvRemoteState } = getState();
    if (playbackMode !== "tv") throw Error("当前不是电视模式");
    if (sending || (tvRemoteState?.pending && !tvRemoteState?.can_override))
      throw Error("上一条指令正在执行");
    const hasSong = ["pause", "resume", "restart", "skip", "replay_next", "audio_mode"].includes(
      action
    );
    sending = true;
    if (action === "pause") optimisticPaused = true;
    if (action === "resume") optimisticPaused = false;
    if (tvRemoteState) {
      tvRemoteState.pending = true;
      tvRemoteState.can_override = false;
    }
    updateRemoteControls();
    try {
      await api("/api/admin/tv/remote", "POST", {
        action,
        song_id: hasSong ? currentAdminSong?.id : null,
        value
      });
    } catch (e) {
      // Only revert the same snapshot; a newer SSE ACK may already have arrived.
      if (tvRemoteState && getState().tvRemoteState === tvRemoteState)
        tvRemoteState.pending = false;
      optimisticPaused = null;
      throw e;
    } finally {
      sending = false;
      updateRemoteControls();
    }
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
  $("remote-audio-mode").onclick = () =>
    remoteAction($("remote-audio-mode"), async () => {
      const mode = getState().tvRemoteState?.report?.audio_mode;
      await sendRemote("audio_mode", mode === "backing" ? "original" : "backing");
    });
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
  const offsetButtons = createOffsetButtons($("admin-remote-offsets"), (button, step) =>
    remoteAction(button, async () => {
      const current = getState().tvRemoteState?.report?.offset;
      await sendRemote("offset", adjustOffset(current, step));
    })
  );
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
