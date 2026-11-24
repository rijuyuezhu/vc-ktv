"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const { test } = require("node:test");

function tvHarness() {
  const nodes = new Map();
  let now = 0,
    nextTimer = 1;
  const timers = new Map();
  const intervals = [];
  const calls = {
    plays: [],
    preloads: 0,
    nexts: 0,
    queue: [],
    current: null,
    active: null,
    reports: 0
  };
  const storage = new Map([["vcktv-smooth-buffer", "1"]]);
  function el(id) {
    if (!nodes.has(id)) {
      const attrs = new Map();
      const classes = new Set();
      const styles = new Map();
      const element = {
        id,
        style: {
          setProperty(name, value) {
            styles.set(name, value);
          },
          getPropertyValue(name) {
            return styles.get(name) || "";
          }
        },
        hidden: false,
        textContent: "",
        value: "100",
        paused: true,
        currentTime: 0,
        duration: 180,
        readyState: 4,
        playbackRate: 1,
        bufferEnd: 0,
        error: null,
        buffered: { length: 1, start: () => 0, end: () => element.bufferEnd },
        classList: {
          add(name) {
            classes.add(name);
          },
          remove(name) {
            classes.delete(name);
          },
          contains(name) {
            return classes.has(name);
          },
          toggle(name, forced) {
            const enabled = forced ?? !classes.has(name);
            if (enabled) classes.add(name);
            else classes.delete(name);
            return enabled;
          }
        },
        replacements: 0,
        replaceChildren() {
          this.replacements++;
        },
        append() {},
        listeners: {},
        addEventListener(name, fn) {
          this.listeners[name] = fn;
        },
        matches() {
          return false;
        },
        closest() {
          return element;
        },
        setAttribute(k, v) {
          attrs.set(k, v);
        },
        getAttribute(k) {
          return attrs.get(k) || null;
        },
        hasAttribute(k) {
          return attrs.has(k);
        },
        removeAttribute(k) {
          attrs.delete(k);
        },
        load() {},
        pause() {
          this.paused = true;
        },
        async play() {
          this.paused = false;
          calls.plays.push(this.id);
        }
      };
      nodes.set(id, element);
    }
    return nodes.get(id);
  }
  const context = {
    document: {
      getElementById: el,
      createElement: () => ({ textContent: "", setAttribute() {} }),
      querySelectorAll: () => []
    },
    location: { search: "?pair=test-pair" },
    TVOverlay: { reset() {}, draw() {}, setup() {}, setOffset() {} },
    requestAnimationFrame: () => 0,
    URLSearchParams,
    performance: { now: () => now },
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value))
    },
    setTimeout: (fn, delay) => {
      const id = nextTimer++;
      timers.set(id, { fn, at: now + delay });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    setInterval: (fn, delay) => {
      intervals.push({ fn, delay });
      return 0;
    },
    fetch: async (url) => ({
      ok: true,
      async json() {
        if (url.includes("/api/tv/report")) {
          calls.reports++;
          return { ok: true };
        }
        if (url.includes("/api/tv/activate")) return { token: "test" };
        if (url.includes("/api/tv/next"))
          return {
            active:
              ++calls.nexts === 1
                ? {
                    id: 1,
                    title: "A",
                    nickname: "guest",
                    media_url: "/media/1",
                    media_file: "1.mp4"
                  }
                : null
          };
        if (url.includes("/api/tv/preload")) {
          calls.preloads++;
          return { candidate: { id: 2, media_url: "/media/2", media_file: "2.mp4" } };
        }
        if (url.includes("/api/tv/state"))
          return {
            queue: calls.queue,
            playback_mode: "tv",
            current: calls.current,
            active: calls.active
          };
        return { ok: true };
      }
    })
  };
  function evaluate(js) {
    return runInNewContext(js, context, { filename: "tv.js" });
  }
  evaluate(readFileSync("static/tv.js", "utf8"));
  async function advance(ms) {
    const end = now + ms;
    while (true) {
      const pending = [...timers]
        .filter(([, v]) => v.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!pending) break;
      const [id, timer] = pending;
      timers.delete(id);
      now = timer.at;
      timer.fn();
      await Promise.resolve();
    }
    now = end;
    await Promise.resolve();
  }
  async function ready() {
    for (let i = 0; i < 30; i++) {
      await Promise.resolve();
      if (!el("start").disabled) return;
    }
    throw Error("TV pairing did not finish");
  }
  return {
    el,
    calls,
    intervals,
    storage,
    evaluate,
    advance,
    ready,
    setFetch: (fn) => {
      context.fetch = (url, ...args) =>
        url.includes("/api/tv/report")
          ? Promise.resolve({
              ok: true,
              async json() {
                calls.reports++;
                return { ok: true };
              }
            })
          : fn(url, ...args);
    }
  };
}

test("new song waits for native 12-second buffer instead of starting at one frame", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  assert.deepEqual(h.calls.plays, []);
  h.el("video").bufferEnd = 11;
  await h.advance(500);
  assert.deepEqual(h.calls.plays, []);
  h.el("video").bufferEnd = 12;
  await h.advance(250);
  assert.deepEqual(h.calls.plays, ["video"]);
  await h.advance(2000);
  assert.equal(h.calls.plays.length, 1);
});

test("bounded timeout allows a non-preloading browser to play", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.advance(11999);
  assert.equal(h.calls.plays.length, 0);
  await h.advance(250);
  assert.deepEqual(h.calls.plays, ["video"]);
});

test("manual resume bypasses buffer; a skipped track cannot play from a stale timer", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  assert.deepEqual(h.calls.plays, ["video"]);
  await h.advance(13000);
  assert.equal(h.calls.plays.length, 1);
  const next = tvHarness();
  await next.ready();
  await next.el("start").onclick();
  await next.evaluate("complete('skip')");
  await next.advance(13000);
  assert.equal(next.calls.plays.length, 0);
});

test("preload only when current song has a reserve; cancel when it runs low", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  h.el("video").duration = 200;
  h.el("video").currentTime = 100;
  h.el("video").bufferEnd = 112;
  await h.evaluate("checkPreload()");
  assert.equal(h.calls.preloads, 0);
  h.el("video").bufferEnd = 123;
  await h.evaluate("checkPreload()");
  assert.equal(h.calls.preloads, 1);
  assert.equal(h.evaluate("preloaded.id"), 2);
  h.el("video").currentTime = 120;
  await h.evaluate("checkPreload()");
  assert.equal(h.evaluate("preloaded"), null);
});

test("default playback cancels speculative preload when current buffer falls low", async () => {
  const h = tvHarness();
  await h.ready();
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  h.el("video").duration = 200;
  h.el("video").currentTime = 140;
  h.el("video").bufferEnd = 154;
  await h.evaluate("checkPreload()");
  assert.equal(h.calls.preloads, 1);
  assert.equal(h.evaluate("preloaded.id"), 2);
  h.el("video").bufferEnd = 146;
  await h.evaluate("checkPreload()");
  assert.equal(h.evaluate("preloaded"), null);
});

test("default TV mode starts immediately without extra caching delay", async () => {
  const h = tvHarness();
  await h.ready();
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  assert.deepEqual(h.calls.plays, ["video"]);
});

test("next song stays visible with empty state and highlights only when the first item changes", async () => {
  const h = tvHarness();
  await h.ready();
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent, "暂无下一首可播放歌曲");
  assert.equal(h.el("next-up-row").classList.contains("highlight"), false);
  h.calls.queue = [
    { id: 2, title: "Second", nickname: "B" },
    { id: 3, title: "Third", nickname: "C" }
  ];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent, "Second  B");
  assert.equal(h.el("next-up-row").classList.contains("highlight"), true);
  await h.advance(2201);
  assert.equal(h.el("next-up-row").classList.contains("highlight"), false);
  h.calls.queue[1] = { id: 4, title: "Fourth", nickname: "D" };
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up-row").classList.contains("highlight"), false);
  h.calls.queue = [h.calls.queue[1], h.calls.queue[0]];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent, "Fourth  D");
  assert.equal(h.el("next-up-row").classList.contains("highlight"), true);
  h.calls.queue = [];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent, "暂无下一首可播放歌曲");
  await h.advance(2201);
  assert.equal(h.el("next-up-row").classList.contains("highlight"), false);
});

test("TV bottom banner transparency updates background live and persists the browser choice", () => {
  const h = tvHarness();
  const opacity = () => h.el("song-banner").style.getPropertyValue("--banner-opacity");
  assert.equal(h.el("banner-transparency").value, "15");
  assert.equal(h.el("banner-transparency-value").textContent, "15%");
  assert.equal(opacity(), "0.85");
  h.el("banner-transparency").value = "65";
  h.el("banner-transparency").oninput();
  assert.equal(opacity(), "0.35");
  assert.equal(h.el("banner-transparency-value").textContent, "65%");
  assert.equal(h.storage.get("vcktv-banner-transparency"), "65");
  h.evaluate("setBannerTransparency(100)");
  assert.equal(opacity(), "0");
  h.evaluate("setBannerTransparency(0)");
  assert.equal(opacity(), "1");
  h.evaluate("setBannerTransparency(150)");
  assert.equal(h.el("banner-transparency").value, "100");
});

test("current and next song share a separator-free title and nickname format", async () => {
  const h = tvHarness();
  await h.ready();
  h.calls.current = { id: 1, title: "Now", nickname: "Alice" };
  h.calls.queue = [{ id: 2, title: "Next", nickname: "Bob" }];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("status").textContent, "Now  Alice");
  assert.equal(h.el("next-up").textContent, "Next  Bob");
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  assert.equal(h.el("status").textContent, "A  guest");
});

test("TV source icon distinguishes NetEase from Bilibili without exposing IDs", async () => {
  const h = tvHarness();
  await h.ready();
  h.calls.current = {
    id: 5,
    title: "沦为尘埃",
    nickname: "Alice",
    source_type: "netease",
    source_id: "3434373208"
  };
  h.calls.queue = [
    { id: 6, title: "Sample", nickname: "Bob", source_type: "bilibili", bvid: "BV0000000009" }
  ];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("status").textContent, "沦为尘埃  Alice");
  assert.equal(h.el("status").getAttribute("data-provider"), "netease");
  assert.equal(h.el("next-up").textContent, "Sample  Bob");
  assert.equal(h.el("next-up").getAttribute("data-provider"), "bilibili");
  assert.equal(h.el("status").textContent.includes("3434373208"), false);
  h.calls.queue = [
    { id: 7, title: "Next", nickname: "Carol", source_type: "netease", source_id: "9876543210" }
  ];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-up").textContent, "Next  Carol");
  assert.equal(h.el("next-up").getAttribute("data-provider"), "netease");
  assert.equal(h.el("next-up").textContent.includes("9876543210"), false);
});

test("TV checks commands at 350ms but sends only one ordinary report per second", async () => {
  const h = tvHarness();
  assert.ok(h.intervals.some(({ delay }) => delay === 350));
  await h.ready();
  const initial = h.calls.reports;
  await h.evaluate("syncQueue()");
  await h.advance(800);
  await h.evaluate("syncQueue()");
  assert.equal(h.calls.reports, initial);
  await h.advance(250);
  await h.evaluate("syncQueue()");
  assert.equal(h.calls.reports, initial + 1);
});

test("frequent TV state polls avoid rebuilding an unchanged next-three-song list", async () => {
  const h = tvHarness();
  await h.ready();
  const initial = h.el("next-list").replacements;
  await h.evaluate("syncQueue()");
  await h.advance(400);
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-list").replacements, initial);
  h.calls.queue = [{ id: 8, title: "新歌", nickname: "A", source_type: "bilibili" }];
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-list").replacements, initial + 1);
  await h.evaluate("syncQueue()");
  assert.equal(h.el("next-list").replacements, initial + 1);
});

test("remote commands acknowledge immediately and only once, even inside heartbeat window", async () => {
  const h = tvHarness();
  await h.ready();
  const before = h.calls.reports;
  h.setFetch(async (url) => ({
    ok: true,
    async json() {
      if (url.includes("/api/tv/state"))
        return {
          playback_mode: "tv",
          current: null,
          queue: [],
          remote_command: { seq: 1, action: "opacity", song_id: null, value: 65 }
        };
      return { ok: true };
    }
  }));
  await h.advance(100);
  await h.evaluate("syncQueue()");
  assert.equal(h.el("banner-transparency").value, "65");
  assert.equal(h.calls.reports, before + 1);
  await h.evaluate("syncQueue()");
  assert.equal(h.calls.reports, before + 1, "stale command is not executed or reported again");
  assert.equal(h.evaluate("lastRemoteSeq"), 1);
});

test("slow TV polling serializes requests and never discards a valid result", async () => {
  const h = tvHarness();
  await h.ready();
  await h.evaluate("syncQueue()");
  const pending = [];
  h.setFetch(() => new Promise((resolve, reject) => pending.push({ resolve, reject })));
  const first = h.evaluate("syncQueue()");
  const overlap = h.evaluate("syncQueue()");
  assert.equal(pending.length, 1);
  await overlap;
  pending[0].resolve({
    ok: true,
    async json() {
      return {
        queue: [{ id: 2, title: "Old", nickname: "A" }],
        current: null,
        playback_mode: "tv"
      };
    }
  });
  await first;
  assert.equal(h.el("next-up").textContent, "Old  A");
  const second = h.evaluate("syncQueue()");
  assert.equal(pending.length, 2);
  pending[1].resolve({
    ok: true,
    async json() {
      return {
        queue: [{ id: 3, title: "New", nickname: "B" }],
        current: null,
        playback_mode: "tv"
      };
    }
  });
  await second;
  assert.equal(h.el("next-up").textContent, "New  B");
});

test("failed TV poll releases slot and next poll can recover", async () => {
  const h = tvHarness();
  await h.ready();
  await h.evaluate("syncQueue()");
  const pending = [];
  h.setFetch(() => new Promise((resolve, reject) => pending.push({ resolve, reject })));
  const first = h.evaluate("syncQueue()");
  const overlap = h.evaluate("syncQueue()");
  assert.equal(pending.length, 1);
  pending[0].reject(new Error("request timed out"));
  await first;
  await overlap;
  assert.equal(h.el("control-error").textContent, "request timed out");
  const second = h.evaluate("syncQueue()");
  assert.equal(pending.length, 2);
  pending[1].resolve({
    ok: true,
    async json() {
      return {
        queue: [{ id: 4, title: "Fresh", nickname: "C" }],
        current: null,
        playback_mode: "tv"
      };
    }
  });
  await second;
  assert.equal(h.el("next-up").textContent, "Fresh  C");
});

test("decoder failure without a distinct baked video stays under admin control", async () => {
  const h = tvHarness();
  await h.ready();
  let finishes = 0;
  h.setFetch(async (url) => {
    if (url.includes("/api/tv/next"))
      return {
        ok: true,
        async json() {
          return {
            active: {
              id: 1,
              title: "AV1 Original",
              nickname: "A",
              prep_state: "original",
              media_file: "1-original.mp4",
              source_file: "1-original.mp4",
              media_url: "/media/1",
              html_overlay: null
            }
          };
        }
      };
    if (url.includes("/api/tv/finish")) finishes++;
    return {
      ok: true,
      async json() {
        return { ok: true };
      }
    };
  });
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  h.el("video").listeners.error();
  await Promise.resolve();
  assert.equal(finishes, 0);
  assert.equal(h.evaluate("activeId"), 1);
  assert.match(h.el("status").textContent, /管理员/);
  assert.equal(h.el("cover").classList.contains("hidden"), false);
});

test("failed TV finish waits for acknowledgement before advancing", async () => {
  const h = tvHarness();
  await h.ready();
  let finishes = 0,
    nexts = 0,
    current = 1;
  h.setFetch(async (url) => {
    if (url.includes("/api/tv/next")) {
      nexts++;
      return {
        ok: true,
        async json() {
          return {
            active: { id: current || 2, title: "Song", nickname: "A", media_url: "/media/1" }
          };
        }
      };
    }
    if (url.includes("/api/tv/finish")) {
      finishes++;
      if (finishes === 1)
        return {
          ok: false,
          status: 503,
          async json() {
            return { detail: "unavailable" };
          }
        };
      current = 2;
      return {
        ok: true,
        async json() {
          return { ok: true };
        }
      };
    }
    if (url.includes("/api/tv/state"))
      return {
        ok: true,
        async json() {
          return { current: { id: current }, queue: [], playback_mode: "tv" };
        }
      };
    return {
      ok: true,
      async json() {
        return { ok: true };
      }
    };
  });
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  await h.evaluate("complete('skip')");
  assert.equal(finishes, 1);
  assert.equal(nexts, 1);
  assert.equal(h.evaluate("activeId"), 1);
  assert.match(h.el("status").textContent, /待确认/);
  await h.advance(2501);
  for (let i = 0; i < 25; i++) await Promise.resolve();
  assert.equal(finishes, 2);
  assert.equal(nexts, 2);
  assert.equal(h.evaluate("activeId"), 2);
});

test("lost finish response reconciles server state without settling twice", async () => {
  const h = tvHarness();
  await h.ready();
  let finishes = 0,
    nexts = 0,
    current = 1;
  h.setFetch(async (url) => {
    if (url.includes("/api/tv/next")) {
      nexts++;
      return {
        ok: true,
        async json() {
          return {
            active: { id: current || 2, title: "Song", nickname: "A", media_url: "/media/1" }
          };
        }
      };
    }
    if (url.includes("/api/tv/finish")) {
      finishes++;
      current = 2;
      return {
        ok: false,
        status: 503,
        async json() {
          return { detail: "response lost" };
        }
      };
    }
    if (url.includes("/api/tv/state"))
      return {
        ok: true,
        async json() {
          return { current: { id: current }, queue: [], playback_mode: "tv" };
        }
      };
    return {
      ok: true,
      async json() {
        return { ok: true };
      }
    };
  });
  h.evaluate("smoothPlayback=false");
  await h.el("start").onclick();
  await h.evaluate("complete()");
  assert.equal(finishes, 1);
  assert.equal(nexts, 2);
  await h.advance(3000);
  assert.equal(finishes, 1);
  assert.equal(h.evaluate("activeId"), 2);
});

test("late subtitle response cannot restart a song already skipped", async () => {
  const h = tvHarness();
  await h.ready();
  let releaseOverlay = null,
    nextCalls = 0;
  h.setFetch(async (url) => {
    if (url.includes("/api/tv/next")) {
      nextCalls++;
      return {
        ok: true,
        async json() {
          return {
            active:
              nextCalls === 1
                ? {
                    id: 1,
                    title: "Previous",
                    nickname: "A",
                    media_url: "/media/1",
                    source_file: "orig.mp4",
                    media_file: "baked.mp4",
                    html_overlay: { timeline_url: "/test-timeline", source_url: "/media/1/source" }
                  }
                : null
          };
        }
      };
    }
    if (url.includes("/test-timeline")) {
      return new Promise((resolve) => {
        releaseOverlay = () =>
          resolve({
            ok: true,
            async json() {
              return { timeline: {}, plan: {}, style: {} };
            }
          });
      });
    }
    return {
      ok: true,
      async json() {
        return { ok: true };
      }
    };
  });
  h.evaluate("smoothPlayback=false");
  const begin = h.el("start").onclick();
  for (let i = 0; i < 12 && !releaseOverlay; i++) await Promise.resolve();
  assert.equal(typeof releaseOverlay, "function");
  await h.evaluate("complete('skip')");
  releaseOverlay();
  await begin;
  assert.equal(h.calls.plays.length, 0);
  assert.equal(h.evaluate("activeId"), null);
});

test("backing mode appears only after its asset is ready, and retains the video clock", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  assert.equal(h.el("audio-mode").hidden, true);
  assert.equal(await h.evaluate('switchAudioMode("backing")'), false);

  h.calls.current = {
    id: 1,
    title: "Song",
    backing: { status: "ready", cache_key: "same-audio", url: "/media/1/backing?token=one" }
  };
  await h.evaluate("syncQueue()");
  assert.equal(h.el("audio-mode").hidden, false);
  assert.equal(h.el("audio-mode").disabled, false);

  h.el("video").currentTime = 65;
  await h.el("audio-mode").onclick();
  assert.equal(h.el("video").muted, true);
  assert.equal(h.el("backing-audio").currentTime, 65);
  assert.equal(h.el("backing-audio").volume, h.el("video").volume);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  assert.equal(h.el("audio-mode").title, "切回原唱");
  assert.equal(h.el("audio-mode-icon").src, "/icons/audio-original.svg");
  assert.equal(h.el("audio-mode").getAttribute("aria-pressed"), "true");

  // Signed links can renew on every state poll without resetting the audio.
  h.calls.current.backing.url = "/media/1/backing?token=two";
  await h.evaluate("syncQueue()");
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  assert.equal(h.el("video").muted, true);

  h.el("video").currentTime = 89;
  h.el("backing-audio").currentTime = 87;
  h.el("video").listeners.timeupdate();
  assert.equal(h.el("backing-audio").currentTime, 87, "avoid thrashing while the audio buffers");
  assert.ok(h.el("backing-audio").playbackRate > 1, "temporarily accelerate lagging audio");
  await h.advance(2200);
  h.el("video").listeners.timeupdate();
  assert.equal(h.el("backing-audio").currentTime, 89, "seek once when genuinely out of sync");
  h.el("volume").value = "45";
  h.el("volume").oninput();
  assert.equal(h.el("backing-audio").volume, 0.45);

  await h.el("audio-mode").onclick();
  assert.equal(h.el("video").muted, false);
  assert.equal(h.el("backing-audio").paused, true);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "original");
});

test("backing follows pause/seek/resume and falls back if its audio stalls", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "a", url: "/media/1/backing?token=a" }
  };
  await h.evaluate("syncQueue()");
  await h.el("audio-mode").onclick();
  const video = h.el("video");
  const audio = h.el("backing-audio");
  video.pause();
  video.listeners.pause();
  assert.equal(audio.paused, true);
  video.currentTime = 35;
  video.listeners.seeked();
  assert.equal(audio.currentTime, 35);
  await video.play();
  video.listeners.play();
  await Promise.resolve();
  assert.equal(audio.paused, false);
  video.listeners.waiting();
  assert.equal(audio.paused, true, "a stalled video must pause its backing track");
  video.listeners.playing();
  // Playback events and resolved play promises can arrive in either order.
  await Promise.resolve();
  await Promise.resolve();
  video.listeners.timeupdate();
  await Promise.resolve();
  assert.equal(audio.paused, false);
  video.playbackRate = 1.25;
  video.listeners.ratechange();
  assert.equal(audio.playbackRate, 1.25);
  audio.readyState = 1;
  audio.listeners.waiting();
  assert.equal(video.paused, true, "pause video while accompaniment buffers");
  assert.equal(video.muted, true, "do not treat waiting as a decode failure");
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  await h.advance(1000);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  audio.readyState = 4;
  audio.listeners.canplay();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(video.paused, false, "resume video after accompaniment can play");
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
});

test("sustained accompaniment buffering falls back to original and resumes video", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "a", url: "/media/1/backing?token=a" }
  };
  await h.evaluate("syncQueue()");
  await h.el("audio-mode").onclick();
  h.el("backing-audio").readyState = 1;
  h.el("backing-audio").listeners.waiting();
  assert.equal(h.el("video").paused, true);
  await h.advance(5200);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "original");
  assert.equal(h.el("video").muted, false);
  assert.equal(h.el("video").paused, false);
});

test("switching songs invalidates a pending audio play and resets to original", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "song1", url: "/media/1/backing?token=a" }
  };
  await h.evaluate("syncQueue()");
  let resolveAudio;
  const audio = h.el("backing-audio");
  audio.play = () =>
    new Promise((resolve) => {
      resolveAudio = resolve;
    });
  const switching = h.el("audio-mode").onclick();
  assert.equal(h.el("video").muted, false); // Original remains audible while loading.
  await h.evaluate('complete("skip")');
  resolveAudio();
  await switching;
  assert.equal(h.evaluate("remoteReport().audio_mode"), "original");
  assert.equal(h.el("video").muted, false);
  assert.equal(audio.paused, true);
  assert.equal(h.el("audio-mode").hidden, true);
});

test("revoking the primary TV restores original sound and drops the backing URL", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "song1", url: "/media/1/backing?token=a" }
  };
  await h.evaluate("syncQueue()");
  await h.el("audio-mode").onclick();
  h.evaluate('losePrimary("revoked")');
  assert.equal(h.evaluate("remoteReport().audio_mode"), "original");
  assert.equal(h.el("video").muted, false);
  assert.equal(h.el("backing-audio").paused, true);
  assert.equal(h.el("audio-mode").hidden, true);
});

test("TV poll uses the signed active view, not the compact queue song record", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = { id: 1, title: "Song", nickname: "A" };
  h.calls.active = {
    id: 1,
    backing: { status: "ready", cache_key: "stable", url: "/media/1/backing?token=one" }
  };
  await h.evaluate("syncQueue()");
  assert.equal(h.el("audio-mode").hidden, false);
  await h.el("audio-mode").onclick();
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  await h.evaluate("syncQueue()");
  assert.equal(h.el("video").muted, true);
});

test("backing decode failure never mutes the original soundtrack", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "x", url: "/media/1/backing?token=x" }
  };
  await h.evaluate("syncQueue()");
  const audio = h.el("backing-audio");
  audio.play = async () => {
    throw Error("Not supported");
  };
  await h.el("audio-mode").onclick();
  assert.equal(h.el("video").muted, false);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "original");
  assert.equal(h.el("control-error").textContent.includes("切回原唱"), true);
});

test("guest-issued audio mode command uses the primary TV and reports its ACK", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "same", url: "/media/1/backing?token=one" }
  };
  await h.evaluate("syncQueue()");
  assert.equal(h.el("video").muted, false);
  await h.evaluate('handleRemote({ seq: 1, action: "audio_mode", song_id: 1, value: "backing" })');
  assert.equal(h.el("video").muted, true);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  assert.equal(h.evaluate("remoteReport().backing_ready"), true);
  assert.equal(h.evaluate("remoteReport().ack_seq"), 1);
  await h.evaluate('handleRemote({ seq: 2, action: "audio_mode", song_id: 1, value: "original" })');
  assert.equal(h.el("video").muted, false);
  assert.equal(h.evaluate("remoteReport().ack_seq"), 2);
  await h.evaluate('handleRemote({ seq: 3, action: "audio_mode", song_id: 2, value: "backing" })');
  assert.equal(h.evaluate("remoteReport().audio_mode"), "original");
  assert.equal(h.evaluate("remoteReport().ack_seq"), 3);
});

test("guest audio command cannot activate unavailable backing audio", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  await h.evaluate('handleRemote({ seq: 1, action: "audio_mode", song_id: 1, value: "backing" })');
  assert.equal(h.el("video").muted, false);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "original");
  assert.equal(h.evaluate("remoteReport().ack_seq"), 1);
});

test("a cancelled audio play during seek is transient and preserves accompaniment", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "source", url: "/media/1/backing?token=one" }
  };
  await h.evaluate("syncQueue()");
  await h.el("audio-mode").onclick();
  const video = h.el("video");
  const audio = h.el("backing-audio");
  audio.play = async () => {
    const error = new Error("The play request was interrupted by a new load request");
    error.name = "AbortError";
    throw error;
  };
  video.currentTime = 60;
  video.seeking = true;
  video.listeners.seeking();
  video.seeking = false;
  video.listeners.seeked();
  for (let i = 0; i < 5; i++) await Promise.resolve();
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  assert.equal(video.muted, true);
  assert.equal(h.el("control-error").textContent, "");
});

test("a video pause interrupting stall recovery retries without losing accompaniment", async () => {
  const h = tvHarness();
  await h.ready();
  await h.el("start").onclick();
  await h.el("pause-resume").onclick();
  h.calls.current = {
    id: 1,
    backing: { status: "ready", cache_key: "a", url: "/media/1/backing?token=a" }
  };
  await h.evaluate("syncQueue()");
  await h.el("audio-mode").onclick();
  const video = h.el("video");
  const audio = h.el("backing-audio");
  audio.readyState = 1;
  audio.listeners.waiting();
  assert.equal(video.paused, true);
  let attempts = 0;
  audio.play = async () => {
    if (++attempts === 1) {
      video.listeners.pause();
      throw Object.assign(new Error("interrupted by pause()"), { name: "AbortError" });
    }
    audio.paused = false;
  };
  audio.readyState = 4;
  audio.listeners.canplay();
  for (let i = 0; i < 8; i++) await Promise.resolve();
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
  assert.equal(video.paused, true);
  await h.advance(100);
  for (let i = 0; i < 8; i++) await Promise.resolve();
  assert.equal(attempts, 2);
  assert.equal(video.paused, false);
  assert.equal(video.muted, true);
  assert.equal(audio.paused, false);
  assert.equal(h.el("control-error").textContent, "");
  await h.advance(5200);
  assert.equal(h.evaluate("remoteReport().audio_mode"), "backing");
});
