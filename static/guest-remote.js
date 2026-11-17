"use strict";

// One compact controller below the current guest's own song card.
export function setupGuestRemote({ container, api, refresh, confirmSkip }) {
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

  let songId = null;
  let remote = null;
  let sending = false;
  let failure = "";
  let generation = 0;

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
  const mode = button("切换伴唱", null);
  const pause = button("暂停", "pause");
  const restart = button("从头重唱", "rotate-ccw");
  const skip = button("跳过自己的歌", "skip-forward");

  async function send(action, value = null) {
    if (!songId || !remote?.available || !remote.online || remote.pending || sending) return;
    if (action === "skip" && !confirmSkip()) return;
    const id = songId;
    const currentGeneration = generation;
    sending = true;
    failure = "";
    update();
    try {
      await api("/api/guest/tv-remote", "POST", { action, song_id: id, value });
      if (generation === currentGeneration) message.textContent = "等待主屏确认…";
    } catch (error) {
      if (generation === currentGeneration) failure = error.message || String(error);
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
    mode.disabled = blocked || (!remote.backing_ready && remote.audio_mode !== "backing");
    for (const el of [pause, restart, skip]) el.disabled = blocked;
    const original = remote.audio_mode === "backing";
    mode.title = original ? "切回原唱" : "切换伴唱";
    mode.setAttribute("aria-label", mode.title);
    mode.textContent = original ? "原唱" : "伴唱";
    mode.classList.toggle("hidden", !remote.backing_ready && !original);
    pause.title = remote.paused ? "继续播放" : "暂停";
    pause.setAttribute("aria-label", pause.title);
    pause.querySelector("img").src = "/icons/" + (remote.paused ? "play" : "pause") + ".svg";
    message.textContent =
      failure || (!remote.online ? "主屏离线" : sending || remote.pending ? "等待主屏确认…" : "");
  }

  function reset() {
    generation++;
    panel.remove();
    remote = null;
    songId = null;
    failure = "";
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
      failure = "";
      sending = false;
    }
    remote = state;
    if (panel.parentElement !== container) container.append(panel);
    update();
  }

  return { render, reset };
}
