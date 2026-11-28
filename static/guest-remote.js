"use strict";

import { createOffsetButtons, clock, offsetLabel, adjustOffset } from "./remote-timing.js";

// One compact controller below the current guest's own song card.
export function setupGuestRemote({ container, api, refresh, confirmSkip, toast }) {
  const panel = document.createElement("div");
  panel.className = "guest-remote";
  const actions = document.createElement("div");
  actions.className = "guest-remote-actions";
  panel.append(actions);
  const message = document.createElement("span");
  message.className = "guest-remote-status";
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  panel.append(message);

  const progress = document.createElement("div");
  progress.className = "remote-progress";
  progress.setAttribute("aria-label", "播放进度");
  panel.append(progress);
  const timing = document.createElement("div");
  timing.className = "guest-remote-timing";
  const label = document.createElement("div");
  label.className = "remote-label";
  const title = document.createElement("span");
  title.textContent = "字幕偏移";
  const offset = document.createElement("span");
  label.append(title, offset);
  timing.append(label);
  const offsets = document.createElement("div");
  offsets.className = "remote-offsets";
  const offsetButtons = createOffsetButtons(offsets, (_button, step) => {
    if (remote?.overlay) return send("offset", adjustOffset(remote.offset, step));
  });
  timing.append(offsets);
  panel.append(timing);

  let songId = null;
  let remote = null;
  let sending = false;
  let generation = 0;
  let lastError = "";
  let lastOnline = null;
  let showTiming = false;
  let stream = null;
  let statusInFlight = false;
  let statusWakeQueued = false;

  async function syncRemote() {
    if (!songId) return;
    if (statusInFlight) {
      statusWakeQueued = true;
      return;
    }
    statusInFlight = true;
    const watchedSong = songId;
    const watchedGeneration = generation;
    try {
      const state = await api("/api/guest/tv-remote");
      if (songId !== watchedSong || generation !== watchedGeneration) return;
      if (!state.available || state.song_id !== watchedSong) {
        reset();
        refresh();
      } else {
        render({ id: watchedSong, mine: true, status: "playing" }, state);
      }
    } catch {
      // The normal page refresh is already the network/SSE fallback.
    } finally {
      statusInFlight = false;
      if (statusWakeQueued) {
        statusWakeQueued = false;
        if (songId) syncRemote();
      }
    }
  }

  function disconnect() {
    stream?.close();
    stream = null;
  }
  function connect() {
    if (stream || typeof EventSource !== "function" || document.visibilityState === "hidden")
      return;
    const source = new EventSource("/api/guest/tv/events");
    stream = source;
    for (const event of ["ready", "update"]) {
      source.addEventListener(event, () => {
        if (stream === source) syncRemote();
      });
    }
    source.addEventListener("revoked", () => {
      if (stream !== source) return;
      disconnect();
      refresh();
    });
    // EventSource reconnects automatically; existing page refresh is the fallback.
  }

  function button(title, icon) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "btn neutral";
    el.title = title;
    el.setAttribute("aria-label", title);
    if (icon) {
      const image = document.createElement("img");
      image.src = "/icons/" + icon + ".svg";
      image.alt = "";
      image.setAttribute("aria-hidden", "true");
      el.append(image);
    }
    actions.append(el);
    return el;
  }
  const mode = button("切换伴奏", "audio-backing");
  const pause = button("暂停", "pause");
  const restart = button("从头重播", "rotate-ccw");
  const skip = button("跳过", "skip-forward");

  async function send(action, value = null) {
    if (!songId || !remote?.available || !remote.online || remote.pending || sending) return;
    if (action === "skip" && !confirmSkip()) return;
    const id = songId;
    const currentGeneration = generation;
    sending = true;
    update();
    try {
      await api("/api/guest/tv-remote", "POST", { action, song_id: id, value });
      if (generation === currentGeneration) message.textContent = "等待主屏确认…";
    } catch (error) {
      if (generation === currentGeneration) toast(error.message || String(error), true);
    } finally {
      if (generation === currentGeneration) {
        sending = false;
        update();
      }
      // A new playback song or a newer admin command may already have taken over.
      refresh();
    }
  }

  mode.onclick = () =>
    send("audio_mode", remote?.audio_mode === "backing" ? "original" : "backing");
  pause.onclick = () => send(remote?.paused ? "resume" : "pause");
  restart.onclick = () => send("restart");
  skip.onclick = () => send("skip");

  function update() {
    if (!remote) return;
    const blocked = sending || remote.pending || !remote.online;
    progress.textContent = remote.online
      ? clock(remote.position) + " / " + clock(remote.duration)
      : "— / —";
    const hasOverlay = remote.online && remote.overlay;
    offset.textContent = hasOverlay ? offsetLabel(remote.offset ?? 0) : "—";
    if (remote.online) showTiming = Boolean(remote.overlay);
    timing.classList.toggle("hidden", !showTiming);
    panel.classList.toggle("remote-pending", remote.online && (sending || remote.pending));
    for (const el of offsetButtons) {
      el.disabled = blocked || !hasOverlay;
      el.setAttribute("aria-busy", String(sending || remote.pending));
    }
    mode.disabled = blocked || (!remote.backing_ready && remote.audio_mode !== "backing");
    for (const el of [pause, restart, skip]) el.disabled = blocked;
    const original = remote.audio_mode === "backing";
    mode.title = original ? "切回原唱" : "切换伴奏";
    mode.querySelector("img").src =
      "/icons/" + (original ? "audio-original" : "audio-backing") + ".svg";
    mode.setAttribute("aria-label", mode.title);
    mode.setAttribute("aria-pressed", String(original));
    mode.classList.toggle("hidden", !remote.backing_ready && !original);
    pause.title = remote.paused ? "继续播放" : "暂停";
    pause.setAttribute("aria-label", pause.title);
    pause.querySelector("img").src = "/icons/" + (remote.paused ? "play" : "pause") + ".svg";
    const status = !remote.online ? "主屏离线" : sending || remote.pending ? "等待主屏确认…" : "";
    for (const button of actions.children) {
      button.setAttribute("aria-busy", String(sending || remote.pending));
      button.setAttribute("aria-disabled", String(button.disabled));
    }
    message.textContent = status;
  }

  function reset() {
    if (songId !== null) generation++;
    disconnect();
    panel.remove();
    remote = null;
    songId = null;
    lastError = "";
    lastOnline = null;
    showTiming = false;
    sending = false;
  }

  function render(song, state) {
    if (
      !song?.mine ||
      song.status !== "playing" ||
      !state?.available ||
      state.song_id !== song.id
    ) {
      reset();
      return;
    }
    if (songId !== song.id) {
      generation++;
      songId = song.id;
      lastError = "";
      lastOnline = null;
      showTiming = false;
      sending = false;
    }
    remote = state;
    if (!state.online && lastOnline !== false) toast("主屏离线", true);
    lastOnline = state.online;
    if (!state.pending) {
      if (state.error && state.error !== lastError) toast(state.error, true);
      lastError = state.error || "";
    }
    if (panel.parentElement !== container) container.append(panel);
    update();
    connect();
  }

  return { render, reset, disconnect };
}
