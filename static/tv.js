"use strict";
let video = document.getElementById("video");
let standby = document.getElementById("preload");
const cover = document.getElementById("cover");
const statusEl = document.getElementById("status");
const start = document.getElementById("start");
const error = document.getElementById("error");
const panel = document.getElementById("controls");
const toggle = document.getElementById("panel-toggle");
const pauseResume = document.getElementById("pause-resume");
const restart = document.getElementById("restart");
const skip = document.getElementById("skip");
const replayNext = document.getElementById("replay-next");
const volume = document.getElementById("volume");
const backingAudio = document.getElementById("backing-audio");
const audioModeButton = document.getElementById("audio-mode");
const banner = document.getElementById("song-banner");
const bannerTransparency = document.getElementById("banner-transparency");
const bannerTransparencyValue = document.getElementById("banner-transparency-value");
const smoothBuffer = document.getElementById("smooth-buffer");
const hideUI = document.getElementById("hide-ui");
const nowPlaying = document.getElementById("now-playing");
const progress = document.getElementById("progress");
const nextList = document.getElementById("next-list");
const nextUp = document.getElementById("next-up");
const nextUpRow = document.getElementById("next-up-row");
const controlError = document.getElementById("control-error");
const captions = document.getElementById("html-captions");
const offsetControls = document.getElementById("offset-controls");
const offsetValue = document.getElementById("offset-value");
let offsetTenths = 0;
let activeOverlay = false;
let activeBakedUrl = null;
function overlaySource(song) {
  return song.html_overlay?.source_url || null;
}
function mediaSource(song) {
  return overlaySource(song) || song.media_url;
}
function resetOverlay() {
  TVOverlay.reset();
  activeOverlay = false;
  offsetTenths = 0;
  offsetValue.textContent = "+0.0s";
  offsetControls.hidden = true;
}
for (const button of document.querySelectorAll("[data-offset]")) {
  button.onclick = () => {
    if (!activeOverlay) return;
    offsetTenths = Math.max(-600, Math.min(600, offsetTenths + Number(button.dataset.offset)));
    TVOverlay.setOffset(offsetTenths);
    offsetValue.textContent = (offsetTenths >= 0 ? "+" : "") + (offsetTenths / 10).toFixed(1) + "s";
    TVOverlay.draw(video.currentTime, video);
    showPanel();
  };
}
async function loadOverlay(song) {
  resetOverlay();
  if (!song.html_overlay) return false;
  try {
    const resp = await fetch(song.html_overlay.timeline_url, { cache: "no-store" });
    if (!resp.ok) throw Error("字幕数据不可用");
    TVOverlay.setup(await resp.json(), captions);
    activeOverlay = true;
    offsetControls.hidden = false;
    return true;
  } catch {
    resetOverlay();
    return false;
  }
}

const tvParams = new URLSearchParams(location.search);
const pair = tvParams.get("pair");
// Keep the control token in this page only: duplicating a TV tab must not
// clone its authority. A refreshed TV needs a new pairing link from admin.
let token = null;
let renewedAt = 0;
let renewing = false;
let activeId = null;
let started = false;
let inFlight = false;
let finishing = false;
let pendingFinish = null;
let finishRetryTimer = null;
let preloaded = null;
let preloadFailed = null;
let preloadInFlight = false;
let lastProbe = -Infinity;
let replayInFlight = false;
let panelTimer = null;
let nextKey = null;
let highlightTimer = null;
let playbackMode = "tv";
let lastRemoteSeq = 0;
let backingOffer = null;
let backingKey = null;
let audioMode = "original";
let audioVersion = 0;
let resumingBacking = false;
let videoBuffering = false;
let lastBackingSeekAt = -Infinity;
let backingStallTimer = null;
let backingStallPausedVideo = false;
let backingRecovering = false;
const BACKING_STALL_TIMEOUT_MS = 5000;
const START_BUFFER_SECONDS = 12;
const START_BUFFER_WAIT_MS = 12000;
let warmup = null;
let warmupTimer = null;
function setBannerTransparency(value) {
  const transparency = Math.max(0, Math.min(100, Number(value) || 0));
  banner.style.setProperty("--banner-opacity", String(1 - transparency / 100));
  bannerTransparency.value = String(transparency);
  bannerTransparencyValue.textContent = transparency + "%";
}
let savedTransparency = 15;
try {
  const saved = localStorage.getItem("vcktv-banner-transparency");
  if (saved !== null && saved.trim() !== "" && Number.isFinite(Number(saved)))
    savedTransparency = Number(saved);
} catch {}
setBannerTransparency(savedTransparency);
bannerTransparency.oninput = () => {
  setBannerTransparency(bannerTransparency.value);
  try {
    localStorage.setItem("vcktv-banner-transparency", bannerTransparency.value);
  } catch {}
};
function setHiddenUI(hidden) {
  hideUI.checked = hidden;
  banner.hidden = hidden;
  toggle.classList.toggle("clean-screen", hidden);
}
try {
  setHiddenUI(localStorage.getItem("vcktv-hide-ui") === "1");
} catch {
  setHiddenUI(false);
}
hideUI.onchange = () => {
  setHiddenUI(hideUI.checked);
  try {
    localStorage.setItem("vcktv-hide-ui", hideUI.checked ? "1" : "0");
  } catch {}
};
let smoothPlayback = false;
try {
  smoothPlayback = localStorage.getItem("vcktv-smooth-buffer") === "1";
} catch {}
smoothBuffer.checked = smoothPlayback;
smoothBuffer.onchange = () => {
  smoothPlayback = smoothBuffer.checked;
  try {
    localStorage.setItem("vcktv-smooth-buffer", smoothPlayback ? "1" : "0");
  } catch {}
  if (!smoothPlayback && warmup) {
    cancelWarmup();
    video.play().catch(() => {
      plainStatus("请手动点击继续播放");
    });
  }
};
function bufferedAhead(element) {
  for (let i = 0; i < element.buffered.length; i++) {
    if (
      element.buffered.start(i) <= element.currentTime + 0.05 &&
      element.currentTime <= element.buffered.end(i)
    ) {
      return Math.max(0, element.buffered.end(i) - element.currentTime);
    }
  }
  return 0;
}
function cancelWarmup() {
  clearTimeout(warmupTimer);
  warmupTimer = null;
  warmup = null;
}
function playAfterBuffer(song) {
  cancelWarmup();
  const element = video;
  const request = { id: song.id, element };
  const startedAt = performance.now();
  warmup = request;
  const tick = async () => {
    if (warmup !== request || activeId !== song.id || video !== element) return;
    const remaining = Number.isFinite(element.duration)
      ? Math.max(0, element.duration - element.currentTime)
      : START_BUFFER_SECONDS;
    const target = Math.min(START_BUFFER_SECONDS, remaining);
    const ahead = bufferedAhead(element);
    if (ahead >= target || performance.now() - startedAt >= START_BUFFER_WAIT_MS || element.error) {
      cancelWarmup();
      showSongLabel(statusEl, song);
      try {
        await element.play();
      } catch {
        if (activeId !== song.id || video !== element) return;
        plainStatus("请点击画面以允许播放声音");
        cover.classList.remove("hidden");
        start.textContent = "▶ 恢复播放";
      }
    } else {
      showSongLabel(statusEl, song);
      statusEl.textContent += "  缓冲中 " + Math.floor(ahead) + "/" + Math.ceil(target) + " 秒";
      warmupTimer = setTimeout(tick, 250);
    }
  };
  tick();
}
function showPanel() {
  panel.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  clearTimeout(panelTimer);
  panelTimer = setTimeout(() => {
    if (!panel.matches(":hover") && !panel.matches(":focus-within")) hidePanel();
  }, 6500);
}
function hidePanel() {
  panel.hidden = true;
  toggle.setAttribute("aria-expanded", "false");
}
toggle.onclick = () => (panel.hidden ? showPanel() : hidePanel());
panel.onpointermove = showPanel;
panel.onpointerleave = () => {
  clearTimeout(panelTimer);
  panelTimer = setTimeout(() => {
    if (!panel.matches(":focus-within")) hidePanel();
  }, 4000);
};
function plainStatus(message) {
  statusEl.textContent = message;
  statusEl.removeAttribute("data-provider");
}
function songLabel(song) {
  return song.title + "  " + (song.nickname || "");
}
function showSongLabel(element, song) {
  element.textContent = songLabel(song);
  element.setAttribute("data-provider", song.source_type === "netease" ? "netease" : "bilibili");
}
function updateNextSong(song) {
  const key = song ? JSON.stringify([song.id, song.title, song.nickname, song.source_type]) : "";
  if (nextKey === key) return;
  if (song) showSongLabel(nextUp, song);
  else {
    nextUp.textContent = "暂无下一首可播放歌曲";
    nextUp.removeAttribute("data-provider");
  }
  if (nextKey !== null) {
    nextUpRow.classList.add("highlight");
    clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => nextUpRow.classList.remove("highlight"), 2200);
  }
  nextKey = key;
}
function clock(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  return Math.floor(seconds / 60) + ":" + String(Math.floor(seconds % 60)).padStart(2, "0");
}
function updateControls() {
  const available = activeId !== null && playbackMode === "tv" && !finishing && !pendingFinish;
  for (const button of [pauseResume, restart, skip, replayNext]) button.disabled = !available;
  replayNext.disabled = !available || replayInFlight;
  const pauseTitle = video.paused ? "继续" : "暂停";
  pauseResume.setAttribute("aria-label", pauseTitle);
  pauseResume.title = pauseTitle;
  document.getElementById("pause-icon").src =
    "/icons/" + (video.paused ? "play" : "pause") + ".svg";
  progress.textContent = clock(video.currentTime) + " / " + clock(video.duration);
  const backingVisible = Boolean(backingOffer) || audioMode === "backing";
  audioModeButton.hidden = !backingVisible;
  audioModeButton.disabled = !available || (!backingOffer && audioMode === "original");
  audioModeButton.title = audioMode === "backing" ? "切回原唱" : "切换伴奏";
  document.getElementById("audio-mode-icon").src =
    "/icons/" + (audioMode === "backing" ? "audio-original" : "audio-backing") + ".svg";
  audioModeButton.setAttribute("aria-label", audioModeButton.title);
  audioModeButton.setAttribute("aria-pressed", String(audioMode === "backing"));
  audioModeButton.closest(".remote-buttons").classList.toggle("has-backing", backingVisible);
}
function clearBackingStall() {
  if (backingStallTimer !== null) clearTimeout(backingStallTimer);
  backingStallTimer = null;
  const resume = backingStallPausedVideo;
  backingStallPausedVideo = false;
  return resume;
}
function originalAudio(message = "") {
  const resume = clearBackingStall();
  audioVersion++;
  audioMode = "original";
  backingAudio.pause();
  backingAudio.playbackRate = video.playbackRate;
  video.muted = false;
  if (message) controlError.textContent = message;
  updateControls();
  if (resume && activeId !== null && !pendingFinish && !video.ended) video.play().catch(() => {});
}
function resetBacking() {
  originalAudio();
  videoBuffering = false;
  backingOffer = null;
  backingKey = null;
  backingAudio.removeAttribute("src");
  backingAudio.load();
  // The standby player may become the active video on the next song.
  standby.muted = false;
  updateControls();
}
function offerBacking(song) {
  if (!song || song.id !== activeId) return;
  const available = song.backing?.status === "ready";
  const offer = available ? song.backing.url : null;
  const key = available ? song.backing.cache_key : null;
  if (!offer && audioMode === "backing") originalAudio("伴奏不可用，已切回原唱");
  if (key !== backingKey && audioMode === "backing") originalAudio();
  backingOffer = offer || null;
  backingKey = key || null;
  updateControls();
}
async function switchAudioMode(mode) {
  if (mode === "original") {
    originalAudio();
    return true;
  }
  if (
    mode !== "backing" ||
    !backingOffer ||
    activeId === null ||
    playbackMode !== "tv" ||
    finishing ||
    pendingFinish
  )
    return false;
  if (audioMode === "backing") return true;
  const songId = activeId;
  const currentVideo = video;
  const url = backingOffer;
  const key = backingKey;
  const version = ++audioVersion;
  try {
    if (backingAudio.getAttribute("src") !== url) {
      backingAudio.src = url;
      backingAudio.load();
    }
    backingAudio.volume = video.volume;
    backingAudio.playbackRate = video.playbackRate;
    backingAudio.currentTime = video.currentTime;
    lastBackingSeekAt = performance.now();
    // Keep the original video audible until the replacement audio can play.
    if (!video.paused) {
      await backingAudio.play();
      // An initial network/decoder delay advances the video but not the AAC.
      // Seek once before changing the audible source, never on every timeupdate.
      if (Math.abs(backingAudio.currentTime - video.currentTime) > 0.15) {
        backingAudio.currentTime = video.currentTime;
        lastBackingSeekAt = performance.now();
        if (backingAudio.readyState < 3)
          await new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
              backingAudio.removeEventListener("canplay", ready);
              reject(Error("伴奏加载超时"));
            }, BACKING_STALL_TIMEOUT_MS);
            function ready() {
              clearTimeout(timer);
              resolve();
            }
            backingAudio.addEventListener("canplay", ready, { once: true });
          });
      }
    }
    if (
      version !== audioVersion ||
      activeId !== songId ||
      video !== currentVideo ||
      backingKey !== key ||
      pendingFinish
    ) {
      if (audioMode !== "backing") backingAudio.pause();
      return false;
    }
    if (video.paused) backingAudio.pause();
    audioMode = "backing";
    video.muted = true;
    updateControls();
    reportRemote("", true).catch(() => {});
    return true;
  } catch {
    if (version === audioVersion) originalAudio("伴奏不可播放，已切回原唱");
    return false;
  }
}
function syncBacking() {
  if (audioMode !== "backing" || activeId === null || backingStallTimer !== null) return;
  try {
    backingAudio.volume = video.volume;
    const drift = video.currentTime - backingAudio.currentTime;
    // Small differences are smoothed by rate adjustment; repeated seeks cause
    // repeated AAC stalls on browsers which buffer after each seek.
    const manualSeek = video.paused || video.seeking;
    if (
      Math.abs(drift) > (manualSeek ? 0.1 : 0.4) &&
      (manualSeek || backingAudio.readyState >= 2) &&
      (manualSeek || performance.now() - lastBackingSeekAt > 2000)
    ) {
      backingAudio.currentTime = video.currentTime;
      lastBackingSeekAt = performance.now();
      backingAudio.playbackRate = video.playbackRate;
    } else {
      const correction = Math.max(-0.08, Math.min(0.08, drift * 0.4));
      backingAudio.playbackRate = video.playbackRate * (1 + correction);
    }
    if (video.paused || video.seeking || videoBuffering || video.readyState < 2) {
      backingAudio.pause();
    } else if (backingAudio.paused && !resumingBacking) {
      const version = audioVersion;
      resumingBacking = true;
      backingAudio
        .play()
        .catch((error) => {
          // A video seek, waiting event, or pause can cancel an in-flight
          // play() with AbortError. This is not a decoder failure.
          if (version === audioVersion && error?.name !== "AbortError")
            originalAudio("伴奏中断，已切回原唱");
        })
        .finally(() => {
          resumingBacking = false;
        });
    }
  } catch {
    originalAudio("伴奏同步失败，已切回原唱");
  }
}
function recoverBackingStall() {
  if (
    backingStallTimer === null ||
    backingRecovering ||
    audioMode !== "backing" ||
    backingAudio.readyState < 3
  )
    return;
  const version = audioVersion;
  const resumeVideo = backingStallPausedVideo;
  backingRecovering = true;
  if (Math.abs(backingAudio.currentTime - video.currentTime) > 0.1) {
    backingAudio.currentTime = video.currentTime;
    lastBackingSeekAt = performance.now();
  }
  backingAudio
    .play()
    .then(() => {
      if (version !== audioVersion) return;
      clearBackingStall();
      if (resumeVideo) return video.play();
    })
    .catch((error) => {
      if (version !== audioVersion) return;
      if (error?.name === "AbortError") {
        // A queued video pause/seek can interrupt this recovery. Retry within
        // the existing stall deadline; a canplay event may already have fired.
        if (backingStallTimer !== null)
          setTimeout(() => {
            if (version === audioVersion) recoverBackingStall();
          }, 100);
      } else originalAudio("伴奏无法恢复，已切回原唱");
    })
    .finally(() => {
      backingRecovering = false;
    });
}
audioModeButton.onclick = async () => {
  await switchAudioMode(audioMode === "backing" ? "original" : "backing");
  showPanel();
};
backingAudio.addEventListener("error", () => {
  if (audioMode === "backing") originalAudio("伴奏加载失败，已切回原唱");
});
backingAudio.addEventListener("waiting", () => {
  if (
    audioMode !== "backing" ||
    backingStallTimer !== null ||
    video.paused ||
    video.seeking ||
    videoBuffering
  )
    return;
  // waiting is normal during a seek or brief rebuffer. Freeze the video
  // clock instead of letting the two tracks drift apart.
  const version = audioVersion;
  backingStallPausedVideo = true;
  video.pause();
  backingStallTimer = setTimeout(() => {
    if (version === audioVersion) originalAudio("伴奏缓冲超时，已切回原唱");
  }, BACKING_STALL_TIMEOUT_MS);
  recoverBackingStall();
});
backingAudio.addEventListener("canplay", recoverBackingStall);
backingAudio.addEventListener("ended", () => {
  if (audioMode === "backing" && !video.ended) originalAudio("伴奏提前结束，已切回原唱");
});
let queueInFlight = false;
let queuedStreamWake = false;
let remoteStream = null;
let remoteStreamReady = false;
let lastQueuePollAt = -Infinity;
let lastRemoteReportAt = -Infinity;
let nextListKey = null;
async function syncQueue(fromStream = false) {
  if (!token) return;
  if (queueInFlight) {
    if (fromStream) queuedStreamWake = true;
    return;
  }
  queueInFlight = true;
  try {
    const state = await api("/api/tv/state");
    lastQueuePollAt = performance.now();
    playbackMode = state.playback_mode || "tv";
    const handled =
      playbackMode === "tv" && state.remote_command && (await handleRemote(state.remote_command));
    const queue = (state.queue || []).slice(0, 3);
    updateNextSong(queue[0]);
    const listKey = JSON.stringify(queue.map((s) => [s.id, s.title, s.nickname, s.source_type]));
    if (listKey !== nextListKey) {
      nextListKey = listKey;
      nextList.replaceChildren();
      if (!queue.length) {
        const empty = document.createElement("li");
        empty.textContent = "暂无下一首可播放歌曲";
        nextList.append(empty);
      }
      for (const [index, song] of queue.entries()) {
        const li = document.createElement("li");
        li.textContent = index + 1 + ". " + songLabel(song);
        li.setAttribute("data-provider", song.source_type === "netease" ? "netease" : "bilibili");
        nextList.append(li);
      }
    }
    if (state.current) {
      showSongLabel(nowPlaying, state.current);
      offerBacking(state.active || state.current);
    } else {
      nowPlaying.textContent = "暂无正在播放的歌曲";
      nowPlaying.removeAttribute("data-provider");
    }
    if (!started) {
      if (state.current) showSongLabel(statusEl, state.current);
      else {
        statusEl.textContent = "等待播放";
        statusEl.removeAttribute("data-provider");
      }
    }
    if (playbackMode !== "tv") controlError.textContent = "当前未选择电视播放";
    else if (controlError.textContent === "当前未选择电视播放") controlError.textContent = "";
    if (preloaded && (!queue.length || preloaded.id !== queue[0].id)) clearPreload();
    updateControls();
    if (!handled) await reportRemote();
  } catch (e) {
    if (e.status === 401 || e.status === 403) losePrimary(e.message);
    else controlError.textContent = e.message || String(e);
  } finally {
    queueInFlight = false;
    if (queuedStreamWake) {
      queuedStreamWake = false;
      syncQueue();
    }
  }
}
function remoteReport(error = "") {
  return {
    ack_seq: lastRemoteSeq,
    active_id: activeId,
    paused: video.paused,
    position: Number.isFinite(video.currentTime) ? video.currentTime : 0,
    duration: Number.isFinite(video.duration) ? video.duration : null,
    volume: Math.round(video.volume * 100),
    offset: offsetTenths,
    opacity: Number(bannerTransparency.value),
    smooth: smoothPlayback,
    hide_ui: hideUI.checked,
    started,
    overlay: activeOverlay,
    audio_mode: audioMode,
    backing_ready: Boolean(backingOffer),
    error: String(error || "").slice(0, 150)
  };
}
async function reportRemote(message = "", force = false) {
  if (!token) return;
  const now = performance.now();
  if (!force && !message && now - lastRemoteReportAt < 1000) return;
  lastRemoteReportAt = now;
  await api("/api/tv/report", "POST", remoteReport(message));
}
async function handleRemote(command) {
  if (command.seq <= lastRemoteSeq) return;
  lastRemoteSeq = command.seq;
  let failure = "";
  try {
    if (pendingFinish && command.song_id !== null) throw Error("播放结束待确认");
    if (command.song_id !== null && command.song_id !== activeId) throw Error("歌曲已切换");
    switch (command.action) {
      case "pause":
        video.pause();
        break;
      case "resume":
        cancelWarmup();
        await video.play();
        break;
      case "audio_mode":
        if (!(await switchAudioMode(command.value))) throw Error("伴奏模式不可用");
        break;
      case "restart":
        cancelWarmup();
        video.currentTime = 0;
        await video.play();
        break;
      case "skip":
        await complete("skip");
        break;
      case "replay_next":
        await api("/api/tv/replay-next", "POST", { song_id: command.song_id });
        break;
      case "volume":
        volume.value = String(command.value);
        video.volume = standby.volume = Number(command.value) / 100;
        syncBacking();
        break;
      case "offset":
        if (!activeOverlay) throw Error("当前没有实时字幕");
        offsetTenths = Math.round(Number(command.value));
        TVOverlay.setOffset(offsetTenths);
        offsetValue.textContent =
          (offsetTenths >= 0 ? "+" : "") + (offsetTenths / 10).toFixed(1) + "s";
        break;
      case "opacity":
        setBannerTransparency(command.value);
        break;
      case "hide_ui":
        hideUI.checked = Boolean(command.value);
        hideUI.onchange();
        break;
      case "smooth":
        smoothBuffer.checked = Boolean(command.value);
        smoothBuffer.onchange();
        break;
      default:
        throw Error("未知遥控命令");
    }
    updateControls();
  } catch (e) {
    failure = e.message || String(e);
  }
  await reportRemote(failure, true);
  return true;
}
async function api(path, method = "GET", body = null) {
  const options = {
    method,
    cache: "no-store",
    headers: { Authorization: "Bearer " + (token || "") }
  };
  if (body) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const err = new Error(result.detail || "请求失败");
    err.status = response.status;
    throw err;
  }
  return result;
}
function losePrimary(message = "主屏权限已撤回，请管理员重新连接") {
  remoteStream?.close();
  remoteStream = null;
  remoteStreamReady = false;
  clearTimeout(finishRetryTimer);
  finishRetryTimer = null;
  pendingFinish = null;
  token = null;
  renewedAt = 0;
  started = false;
  activeId = null;
  inFlight = false;
  cancelWarmup();
  clearPreload();
  resetOverlay();
  resetBacking();
  activeBakedUrl = null;
  for (const element of [video, standby]) {
    element.pause();
    element.removeAttribute("src");
    element.load();
  }
  cover.classList.remove("hidden");
  start.disabled = true;
  start.textContent = "主屏不可用";
  plainStatus("此页面已失去主屏权限");
  error.textContent = message;
  updateControls();
}
function connectRemoteStream() {
  if (!token || remoteStream || typeof EventSource !== "function") return;
  const stream = new EventSource("/api/tv/events");
  remoteStream = stream;
  for (const name of ["ready", "update"]) {
    stream.addEventListener(name, () => {
      if (remoteStream !== stream) return;
      remoteStreamReady = true;
      syncQueue(true);
    });
  }
  stream.addEventListener("revoked", () => losePrimary());
  stream.onerror = () => {
    if (remoteStream === stream) remoteStreamReady = false;
    // EventSource reconnects; the 350 ms HTTP poll is available meanwhile.
  };
}

async function connectPrimary() {
  if (!pair) return;
  start.disabled = true;
  try {
    const response = await fetch("/api/tv/activate", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pair })
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.detail || "主屏配对失败");
    token = result.token;
    renewedAt = performance.now();
    error.textContent = "";
    start.textContent = "▶ 开始";
    connectRemoteStream();
    await syncQueue();
    if (token) start.disabled = false;
  } catch (e) {
    losePrimary(e.message || String(e));
  }
}
async function renewPrimary() {
  if (!token || renewing) return;
  renewing = true;
  try {
    const updated = await api("/api/tv/renew", "POST");
    if (!token) return;
    token = updated.token;
    renewedAt = performance.now();
    // Existing EventSource still holds the old token from its initial cookie.
    remoteStream?.close();
    remoteStream = null;
    remoteStreamReady = false;
    connectRemoteStream();
  } catch (e) {
    if (e.status === 401 || e.status === 403) losePrimary(e.message);
  } finally {
    renewing = false;
  }
}
function fail(e) {
  if (e.status === 401 || e.status === 403) {
    losePrimary(e.message);
    return;
  }
  const message = e.message || String(e);
  if (message.includes("手动投屏")) {
    error.textContent = "";
    plainStatus("当前使用手动投屏，请在管理后台切歌");
    return;
  }
  error.textContent = message;
  plainStatus("连接异常，正在重试");
}
function clearPreload() {
  preloaded = null;
  if (standby.hasAttribute("src")) {
    standby.removeAttribute("src");
    standby.load();
  }
}
function preloadKey(song) {
  // Signed URLs change expiry between polls. Compare stable media identity instead.
  return JSON.stringify([
    song.id,
    song.media_file,
    song.source_file || "",
    Boolean(song.html_overlay),
    song.html_overlay?.source_key || ""
  ]);
}
async function checkPreload() {
  if (
    !started ||
    activeId === null ||
    inFlight ||
    preloadInFlight ||
    !Number.isFinite(video.duration)
  )
    return;
  const remaining = video.duration - video.currentTime;
  if (remaining <= 0) return;
  const ahead = bufferedAhead(video);
  // Aggressive preloading must not starve the current 4 Mbps stream.
  if (preloaded && ahead < Math.min(8, remaining)) clearPreload();
  if (remaining > Math.min(smoothPlayback ? 120 : 60, video.duration / 2)) return;
  if (ahead < Math.min(smoothPlayback ? 20 : 10, remaining)) return;
  const now = performance.now();
  if (now - lastProbe < 8000) return;
  lastProbe = now;
  const playingId = activeId;
  preloadInFlight = true;
  try {
    const { candidate } = await api("/api/tv/preload");
    if (activeId !== playingId || !started) return;
    if (!candidate) {
      clearPreload();
      return;
    }
    const key = preloadKey(candidate);
    if (preloaded && preloadKey(preloaded) === key) return;
    clearPreload();
    if (preloadFailed === key) return;
    preloaded = candidate;
    standby.src = mediaSource(candidate);
    standby.load();
  } catch {
    // Speculative loading must never interrupt the current song.
  } finally {
    preloadInFlight = false;
  }
}
async function next() {
  if (!token || playbackMode !== "tv" || inFlight || finishing || !started || activeId !== null)
    return;
  inFlight = true;
  try {
    const answer = await api("/api/tv/next", "POST");
    if (!answer.active) {
      plainStatus("正在等待下一首歌……");
      cover.classList.remove("hidden");
      return;
    }
    resetBacking();
    activeId = answer.active.id;
    offerBacking(answer.active);
    cancelWarmup();
    const useOverlay = await loadOverlay(answer.active);
    if (!token || activeId !== answer.active.id || pendingFinish) {
      resetOverlay();
      return;
    }
    activeBakedUrl =
      useOverlay && answer.active.media_file !== answer.active.source_file
        ? answer.active.media_url
        : null;
    const sourceUrl = useOverlay ? mediaSource(answer.active) : answer.active.media_url;
    if (!sourceUrl) {
      video.pause();
      clearPreload();
      cover.classList.remove("hidden");
      resetBacking();
      resetOverlay();
      activeId = null;
      plainStatus("字幕加载失败，正在重试");
      return;
    }
    if (
      preloaded &&
      Boolean(preloaded.html_overlay) === useOverlay &&
      preloadKey(preloaded) === preloadKey(answer.active) &&
      !standby.error
    ) {
      const old = video;
      video = standby;
      standby = old;
      video.classList.remove("preloading");
      standby.classList.add("preloading");
      preloaded = null;
    } else {
      clearPreload();
      video.src = sourceUrl;
    }
    preloadFailed = null;
    lastProbe = -Infinity;
    showSongLabel(statusEl, answer.active);
    cover.classList.add("hidden");
    updateControls();
    syncQueue();
    if (smoothPlayback) playAfterBuffer(answer.active);
    else {
      try {
        await video.play();
      } catch {
        plainStatus("请点击画面以允许播放声音");
        cover.classList.remove("hidden");
        start.textContent = "▶ 恢复播放";
      }
    }
  } catch (e) {
    fail(e);
  } finally {
    inFlight = false;
  }
}
async function settleFinish() {
  if (!pendingFinish || finishing || !token) return;
  finishing = true;
  const { id, reason } = pendingFinish;
  let settled = false;
  try {
    await api("/api/tv/finish", "POST", { song_id: id, reason });
    settled = true;
  } catch (e) {
    if (e.status === 401 || e.status === 403) {
      losePrimary(e.message);
      return;
    }
    try {
      const state = await api("/api/tv/state");
      settled = state.current?.id !== id; // A lost response may follow a committed finish.
    } catch (lookupError) {
      if (lookupError.status === 401 || lookupError.status === 403) {
        losePrimary(lookupError.message);
        return;
      }
    }
    if (!settled) {
      plainStatus("播放结束待确认，正在重试");
      if (finishRetryTimer === null) {
        finishRetryTimer = setTimeout(() => {
          finishRetryTimer = null;
          settleFinish();
        }, 2500);
      }
    }
  } finally {
    finishing = false;
    updateControls();
  }
  if (!settled) return;
  pendingFinish = null;
  clearTimeout(finishRetryTimer);
  finishRetryTimer = null;
  activeId = null;
  await next();
  syncQueue();
}
async function complete(reason = "") {
  if (activeId === null || pendingFinish) return;
  pendingFinish = { id: activeId, reason };
  resetBacking();
  cancelWarmup();
  resetOverlay();
  activeBakedUrl = null;
  video.pause();
  video.removeAttribute("src");
  video.load();
  updateControls();
  await settleFinish();
}
for (const element of [video, standby]) {
  element.addEventListener("ended", () => {
    if (element === video) complete();
  });
  element.addEventListener("error", () => {
    if (element !== video) {
      if (preloaded) preloadFailed = preloadKey(preloaded);
      clearPreload();
      return;
    }
    if (activeId !== null && !pendingFinish) {
      if (audioMode === "backing") originalAudio();
      if (activeOverlay && activeBakedUrl) {
        const fallback = activeBakedUrl;
        const position = video.currentTime;
        resetOverlay();
        activeBakedUrl = null;
        video.src = fallback;
        video.addEventListener(
          "loadedmetadata",
          () => {
            video.currentTime = position;
            video.play().catch(fail);
          },
          { once: true }
        );
        plainStatus("原片不可播放，已回退烧录版");
      } else {
        video.pause();
        plainStatus("视频无法播放，请管理员更换 H.264 视频或跳过");
        cover.classList.remove("hidden");
        start.textContent = "▶ 重试播放";
      }
    }
  });
  element.addEventListener("timeupdate", () => {
    if (element === video) {
      checkPreload();
      syncBacking();
      updateControls();
    }
  });
  element.addEventListener("play", () => {
    if (element === video) {
      syncBacking();
      updateControls();
    }
  });
  element.addEventListener("pause", () => {
    if (element === video) {
      backingAudio.pause();
      updateControls();
    }
  });
  for (const event of ["seeking", "waiting"]) {
    element.addEventListener(event, () => {
      if (element === video) {
        videoBuffering = true;
        backingAudio.pause();
      }
    });
  }
  for (const event of ["seeked", "playing"]) {
    element.addEventListener(event, () => {
      if (element === video) {
        videoBuffering = false;
        if (event === "seeked") lastBackingSeekAt = -Infinity;
        syncBacking();
      }
    });
  }
  for (const event of ["ratechange", "volumechange"]) {
    element.addEventListener(event, () => {
      if (element === video) syncBacking();
    });
  }
}
start.onclick = async () => {
  if (!token || pendingFinish) return;
  started = true;
  error.textContent = "";
  if (activeId !== null) {
    cancelWarmup();
    cover.classList.add("hidden");
    if (video.error) video.load();
    try {
      await video.play();
    } catch (e) {
      fail(e);
    }
    return;
  }
  await next();
};
setInterval(async () => {
  if (!started || playbackMode !== "tv" || inFlight || activeId !== null) return;
  await next();
}, 2000);
setInterval(checkPreload, 8000);

pauseResume.onclick = async () => {
  if (activeId === null || playbackMode !== "tv" || pendingFinish) return;
  if (video.paused) {
    cancelWarmup();
    try {
      await video.play();
    } catch (e) {
      controlError.textContent = e.message;
    }
  } else video.pause();
  showPanel();
  updateControls();
};
restart.onclick = async () => {
  if (activeId === null || finishing || pendingFinish || playbackMode !== "tv") return;
  cancelWarmup();
  video.currentTime = 0;
  try {
    await video.play();
  } catch (e) {
    controlError.textContent = e.message;
  }
  showPanel();
  updateControls();
};
skip.onclick = () => {
  if (activeId === null || finishing || pendingFinish || playbackMode !== "tv") return;
  complete("skip");
  showPanel();
};
replayNext.onclick = async () => {
  if (activeId === null || finishing || pendingFinish || replayInFlight || playbackMode !== "tv")
    return;
  replayInFlight = true;
  updateControls();
  try {
    await api("/api/tv/replay-next", "POST", { song_id: activeId });
    controlError.textContent = "已安排当前歌曲下一首重唱";
    await syncQueue();
  } catch (e) {
    controlError.textContent = e.message || String(e);
  } finally {
    replayInFlight = false;
    updateControls();
  }
  showPanel();
};
volume.oninput = () => {
  video.volume = standby.volume = Number(volume.value) / 100;
  syncBacking();
  showPanel();
};
// SSE carries prompt notifications; polling reconciles queue state and handles old TVs or outages.
setInterval(() => {
  if (!remoteStreamReady || performance.now() - lastQueuePollAt >= 1000) syncQueue();
}, 350);
setInterval(() => {
  if (token && performance.now() - renewedAt >= 30 * 60 * 1000) renewPrimary();
}, 60000);
setInterval(async () => {
  if (!remoteStreamReady || !token || queueInFlight) return;
  try {
    await reportRemote();
  } catch (e) {
    if (e.status === 401 || e.status === 403) losePrimary(e.message);
  }
}, 1000);
if (pair) connectPrimary();
else losePrimary("请从管理员页面重新打开主屏链接");
updateControls();

function overlayFrame() {
  if (activeOverlay) TVOverlay.draw(video.currentTime, video);
  requestAnimationFrame(overlayFrame);
}
requestAnimationFrame(overlayFrame);
