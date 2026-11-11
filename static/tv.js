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
const banner = document.getElementById("song-banner");
const bannerTransparency = document.getElementById("banner-transparency");
const bannerTransparencyValue = document.getElementById("banner-transparency-value");
const smoothBuffer = document.getElementById("smooth-buffer");
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
}
let queueInFlight = false;
let lastRemoteReportAt = -Infinity;
let nextListKey = null;
async function syncQueue() {
  if (!token || queueInFlight) return;
  queueInFlight = true;
  try {
    const state = await api("/api/tv/state");
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
    if (state.current) showSongLabel(nowPlaying, state.current);
    else {
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
    started,
    overlay: activeOverlay,
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
  const options = { method, cache: "no-store" };
  if (body) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const join = path.includes("?") ? "&" : "?";
  const response = await fetch(path + join + "token=" + encodeURIComponent(token || ""), options);
  const result = await response.json();
  if (!response.ok) {
    const err = new Error(result.detail || "请求失败");
    err.status = response.status;
    throw err;
  }
  return result;
}
function losePrimary(message = "主屏权限已撤回，请管理员重新连接") {
  clearTimeout(finishRetryTimer);
  finishRetryTimer = null;
  pendingFinish = null;
  token = null;
  started = false;
  activeId = null;
  inFlight = false;
  cancelWarmup();
  clearPreload();
  resetOverlay();
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
    error.textContent = "";
    start.textContent = "▶ 开始";
    await syncQueue();
    if (token) start.disabled = false;
  } catch (e) {
    losePrimary(e.message || String(e));
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
    Boolean(song.html_overlay)
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
    activeId = answer.active.id;
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
      updateControls();
    }
  });
  element.addEventListener("play", () => {
    if (element === video) updateControls();
  });
  element.addEventListener("pause", () => {
    if (element === video) updateControls();
  });
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
  showPanel();
};
// Read commands promptly; send ordinary status/heartbeat reports only once per second.
setInterval(syncQueue, 350);
if (pair) connectPrimary();
else losePrimary("请从管理员页面重新打开主屏链接");
updateControls();

function overlayFrame() {
  if (activeOverlay) TVOverlay.draw(video.currentTime, video);
  requestAnimationFrame(overlayFrame);
}
requestAnimationFrame(overlayFrame);
