"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const moduleUrl = pathToFileURL(path.join(__dirname, "../static/admin-playback.js")).href;

function node(tag = "div") {
  const attrs = new Map();
  const el = {
    tagName: tag.toUpperCase(),
    className: "",
    children: [],
    textContent: "",
    value: "",
    disabled: false,
    checked: false,
    paused: true,
    volume: 0.6,
    currentTime: 0,
    duration: 90,
    events: {},
    loads: 0,
    plays: 0
  };
  const classes = () => new Set(el.className.split(/\s+/).filter(Boolean));
  el.classList = {
    contains(name) {
      return classes().has(name);
    },
    toggle(name, force) {
      const s = classes(),
        add = force ?? !s.has(name);
      if (add) s.add(name);
      else s.delete(name);
      el.className = [...s].join(" ");
      return add;
    },
    add(name) {
      el.classList.toggle(name, true);
    },
    remove(name) {
      el.classList.toggle(name, false);
    }
  };
  el.append = (...items) => {
    el.children.push(...items);
  };
  el.replaceChildren = (...items) => {
    el.children = [...items];
  };
  el.setAttribute = (name, value) => attrs.set(name, String(value));
  el.removeAttribute = (name) => {
    attrs.delete(name);
    if (name === "src") el.src = "";
  };
  el.getAttribute = (name) => attrs.get(name) ?? null;
  el.addEventListener = (type, handler) => {
    el.events[type] = handler;
  };
  el.querySelector = (selector) => (selector === "img" ? (el.image ??= { src: "" }) : null);
  el.closest = () => el;
  el.getClientRects = () => [{}];
  el.load = () => {
    el.loads++;
  };
  el.play = async () => {
    el.plays++;
    el.paused = false;
  };
  el.pause = () => {
    el.paused = true;
  };
  return el;
}
function song(id = 1, source = "bilibili") {
  return {
    id,
    title: `Song ${id}`,
    nickname: "Singer",
    source_type: source,
    source_id: source === "netease" ? "123456" : null,
    bvid: source === "bilibili" ? "BV1abc" : "",
    media_url: `/media/${id}.mp4`
  };
}
async function harness() {
  const prior = {
    document: global.document,
    window: global.window,
    fetch: global.fetch,
    confirm: global.confirm,
    prompt: global.prompt,
    setTimeout: global.setTimeout,
    clearTimeout: global.clearTimeout
  };
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(global, "navigator");
  const nodes = new Map(),
    calls = [],
    copied = [],
    messages = [];
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  const state = {
    admin: true,
    mode: "tv",
    tv: { active: false, can_manage: true },
    remote: { online: false },
    next: null,
    allow: true,
    refreshes: 0,
    invalidations: 0,
    failMode: false
  };
  const player = byId("player");
  const remote = node();
  global.document = {
    getElementById: byId,
    createElement: node,
    createTextNode: (text) => ({ textContent: text }),
    querySelector: (selector) => (selector === ".playback-remote" ? remote : null),
    querySelectorAll: () => [],
    visibilityState: "visible",
    body: node(),
    activeElement: null
  };
  global.window = {
    location: { pathname: "/admin", href: "https://ktv.example/admin", hash: "" },
    open: () => ({ location: {}, close() {} })
  };
  Object.defineProperty(global, "navigator", {
    configurable: true,
    value: { clipboard: { writeText: async (value) => copied.push(value) } }
  });
  global.setTimeout = () => 1;
  global.clearTimeout = () => {};
  global.confirm = () => state.allow;
  global.prompt = (...args) => messages.push(args);
  global.fetch = async (url, opts) => {
    const method = opts?.method ?? "GET";
    calls.push({ url, method, body: opts?.body });
    let data = {};
    if (url === "/api/admin/tv") data = state.tv;
    if (url === "/api/admin/tv/remote") data = state.remote;
    if (url === "/api/admin/next") data = { active: state.next };
    if (url === "/api/admin/playback-mode" && state.failMode)
      return {
        ok: false,
        status: 409,
        async json() {
          return { detail: "cannot change mode" };
        }
      };
    if (url.startsWith("/api/admin/tv-link")) data = { url: "/tv-session" };
    return {
      ok: true,
      async json() {
        return data;
      }
    };
  };
  const { setupAdminPlayback } = await import(moduleUrl);
  const playback = setupAdminPlayback({
    refresh: async () => {
      state.refreshes++;
    },
    isAdminPath: () => true,
    getAdmin: () => state.admin,
    addSourceTag: (parent, item) => {
      const tag = node("source");
      tag.title = item.source_type;
      parent.append(tag);
    },
    invalidateAdminCache: () => {
      state.invalidations++;
    }
  });
  const sync = async (mode, active = null, changed = true, next = []) => {
    state.mode = mode;
    await playback.syncFromAdminState({ playback_mode: mode, active, queue: next }, changed);
  };
  return {
    playback,
    sync,
    player,
    state,
    calls,
    copied,
    messages,
    byId,
    cleanup() {
      Object.assign(global, prior);
      if (navigatorDescriptor) Object.defineProperty(global, "navigator", navigatorDescriptor);
      else delete global.navigator;
    }
  };
}

test("browser playback synchronizes active media, download/source actions and avoids reloads", async () => {
  const h = await harness();
  try {
    await h.sync("browser", song(1));
    assert.equal(h.byId("playback-mode").value, "browser");
    assert.equal(h.byId("active-playback-mode").textContent, "浏览器播放");
    assert.equal(h.player.src, "/media/1.mp4");
    assert.equal(h.player.loads, 1);
    assert.equal(h.byId("current-song-download").href, "/api/songs/1/download");
    assert.equal(h.byId("current-song-copy-source").textContent, "⧉ 复制BV 号");
    assert.equal(h.byId("playing-title").classList.contains("playing-link"), true);
    await h.byId("current-song-copy-source").onclick();
    assert.deepEqual(h.copied, ["BV1abc"]);
    await h.sync("browser", song(1), false);
    assert.equal(h.player.loads, 1, "same active media must not reload");
    await h.sync("browser", song(2, "netease"));
    assert.equal(h.player.src, "/media/2.mp4");
    assert.equal(h.byId("current-song-copy-source").textContent, "⧉ 复制网易云链接");
    await h.byId("current-song-copy-source").onclick();
    assert.equal(h.copied[1], "https://music.163.com/#/song?id=123456");
  } finally {
    h.cleanup();
  }
});

test("browser start and finish retain the original finish API and player cleanup", async () => {
  const h = await harness();
  try {
    await h.sync("browser", null);
    h.state.next = song(4);
    await h.byId("start-btn").onclick();
    assert.ok(h.calls.some((c) => c.method === "POST" && c.url === "/api/admin/next"));
    assert.equal(h.player.src, "/media/4.mp4");
    assert.equal(h.player.plays, 1);
    await h.byId("finish-btn").onclick();
    const finished = h.calls.find((c) => c.url === "/api/admin/finish");
    assert.deepEqual(JSON.parse(finished.body), { song_id: 4, reason: "" });
    assert.equal(h.player.src, "");
    assert.equal(h.byId("playing-title").textContent, "当前没有正在播放的歌曲。");
    assert.equal(h.state.refreshes, 2);
  } finally {
    h.cleanup();
  }
});

test("manual mode finishes and claims the next song; skip keeps the original reason", async () => {
  const h = await harness();
  try {
    await h.sync("manual", song(6));
    assert.equal(h.byId("active-playback-mode").textContent, "相册播放");
    assert.equal(h.byId("start-btn").textContent, "领取下一首");
    assert.equal(h.byId("finish-btn").textContent, "唱完并切下一首");
    h.state.next = song(7);
    await h.byId("finish-btn").onclick();
    assert.deepEqual(JSON.parse(h.calls.find((c) => c.url === "/api/admin/finish").body), {
      song_id: 6,
      reason: ""
    });
    assert.equal(h.byId("playing-title").classList.contains("playing-link"), true);
    assert.ok(h.calls.some((c) => c.url === "/api/admin/next"));
    await h.byId("skip-btn").onclick();
    const finished = h.calls.filter((c) => c.url === "/api/admin/finish");
    assert.deepEqual(JSON.parse(finished[1].body), { song_id: 7, reason: "skip" });
  } finally {
    h.cleanup();
  }
});

test("changing playback mode resets current media and invalidates parent cache", async () => {
  const h = await harness();
  try {
    await h.sync("browser", song(8));
    h.byId("playback-mode").value = "manual";
    await h.byId("playback-mode").onchange();
    assert.ok(
      h.calls.some(
        (c) =>
          c.url === "/api/admin/playback-mode" &&
          c.method === "PUT" &&
          JSON.parse(c.body).mode === "manual"
      )
    );
    assert.equal(h.player.src, "");
    assert.equal(h.state.invalidations, 1);
    assert.equal(h.state.refreshes, 1);
    assert.equal(h.byId("active-playback-mode").textContent, "相册播放");
  } finally {
    h.cleanup();
  }
});

test("TV primary owns display, remote state, and never claims media locally", async () => {
  const h = await harness();
  try {
    h.state.tv = { active: true, can_manage: true };
    h.state.remote = {
      online: true,
      can_manage: true,
      report: { active_id: 3, paused: false, volume: 40, duration: 180, position: 36 }
    };
    await h.sync("tv", song(3), true, [song(4)]);
    assert.equal(h.player.src, undefined);
    assert.equal(h.byId("tv-primary-management").classList.contains("hidden"), false);
    assert.equal(h.byId("tv-link-btn").textContent, "▣ 更换主屏");
    assert.equal(h.byId("tv-coming").textContent, "下一首：Song 4");
    assert.equal(h.byId("tv-online").textContent, "主屏在线");
    assert.equal(h.byId("remote-pause").disabled, false);
    await h.byId("remote-pause").onclick();
    assert.ok(
      h.calls.some(
        (c) =>
          c.url === "/api/admin/tv/remote" &&
          c.method === "POST" &&
          JSON.parse(c.body).action === "pause"
      )
    );
    assert.equal(
      h.calls.some((c) => c.url === "/api/admin/next"),
      false
    );
    await h.playback.pollTVRemoteStatus();
    assert.ok(
      h.calls.filter((c) => c.url === "/api/admin/tv/remote" && c.method === "GET").length >= 2
    );
  } finally {
    h.cleanup();
  }
});

test("browser logout can be declined, and accepted logout skips the playing song", async () => {
  const h = await harness();
  try {
    await h.sync("browser", song(9));
    h.state.allow = false;
    assert.equal(await h.playback.prepareLogout(), false);
    assert.equal(
      h.calls.some((c) => c.url === "/api/admin/finish"),
      false
    );
    h.state.allow = true;
    assert.equal(await h.playback.prepareLogout(), true);
    assert.deepEqual(JSON.parse(h.calls.find((c) => c.url === "/api/admin/finish").body), {
      song_id: 9,
      reason: "skip"
    });
    h.playback.resetPlayer();
    assert.equal(h.player.src, "");
  } finally {
    h.cleanup();
  }
});

test("failed mode change preserves the prior playback mode and media", async () => {
  const h = await harness();
  try {
    await h.sync("browser", song(10));
    h.state.failMode = true;
    h.byId("playback-mode").value = "manual";
    await h.byId("playback-mode").onchange();
    assert.equal(h.byId("playback-mode").value, "browser");
    assert.equal(h.player.src, "/media/10.mp4");
    assert.equal(h.state.invalidations, 0);
    assert.equal(h.state.refreshes, 0);
    assert.equal(h.byId("toast").textContent, "cannot change mode");
  } finally {
    h.cleanup();
  }
});

test("non-owner TV administrator cannot manage primary or send remote commands", async () => {
  const h = await harness();
  try {
    h.state.tv = { active: true, can_manage: false };
    h.state.remote = { online: true, can_manage: false, report: { active_id: 11, paused: false } };
    await h.sync("tv", song(11));
    assert.equal(h.byId("tv-primary-status").textContent, "由全局管理员创建");
    assert.equal(h.byId("tv-link-btn").classList.contains("hidden"), true);
    assert.equal(h.byId("tv-copy-btn").classList.contains("hidden"), true);
    assert.equal(h.byId("tv-revoke-btn").classList.contains("hidden"), true);
    assert.equal(h.byId("remote-pause").disabled, true);
    h.state.admin = false;
    const n = h.calls.filter((c) => c.url === "/api/admin/tv/remote").length;
    await h.playback.pollTVRemoteStatus();
    assert.equal(h.calls.filter((c) => c.url === "/api/admin/tv/remote").length, n);
  } finally {
    h.cleanup();
  }
});
