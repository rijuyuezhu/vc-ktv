"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const url = pathToFileURL(path.join(__dirname, "../static/song-requests.js")).href;

function element() {
  const el = {
    value: "",
    textContent: "",
    hidden: false,
    children: [],
    listeners: {},
    attrs: {},
    disabled: false,
    src: ""
  };
  const classes = new Set();
  el.classList = {
    add: (n) => classes.add(n),
    remove: (n) => classes.delete(n),
    contains: (n) => classes.has(n),
    toggle(n, flag) {
      if (flag) classes.add(n);
      else classes.delete(n);
    }
  };
  el.addEventListener = (name, fn) => {
    el.listeners[name] = fn;
  };
  el.setAttribute = (name, value) => {
    el.attrs[name] = value;
  };
  el.replaceChildren = (...children) => {
    el.children = [...children];
  };
  el.append = (...children) => {
    el.children.push(...children);
  };
  el.querySelector = (selector) =>
    selector === ".netease-load-more"
      ? (el.children.find((x) => x.className?.includes("netease-load-more")) ?? null)
      : null;
  el.remove = () => {};
  return el;
}
async function harness() {
  const original = {
    document: global.document,
    fetch: global.fetch,
    setTimeout: global.setTimeout,
    clearTimeout: global.clearTimeout
  };
  const nodes = new Map(),
    calls = [],
    pickers = [];
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  let rejectOrder = false;
  let pageCount = 1;
  const choices = [];
  let previewClosed = 0;
  const videoPreview = {
    choose(info, page, submit) {
      choices.push({ info, page, submit });
    },
    close() {
      previewClosed++;
    }
  };
  global.document = { getElementById: byId, createElement: element };
  global.setTimeout = () => 1;
  global.clearTimeout = () => {};
  global.fetch = async (uri, opts = {}) => {
    calls.push({ uri, opts });
    if (uri.startsWith("/api/preview?")) {
      const raw = new URLSearchParams(uri.split("?")[1]).get("bvid");
      const bvid = raw.match(/BV[0-9A-Za-z]{10}/)?.[0] || raw;
      return {
        ok: true,
        json: async () => ({
          bvid,
          title: "B Track",
          page: 1,
          pages: Array.from({ length: pageCount }, (_, i) => ({
            page: i + 1,
            part: `Part ${i + 1}`
          }))
        })
      };
    }
    if (uri === "/api/songs" && rejectOrder)
      return { ok: false, status: 409, json: async () => ({ detail: "submission failed" }) };
    if (uri === "/api/netease/songs") {
      if (rejectOrder)
        return { ok: false, status: 409, json: async () => ({ detail: "submission failed" }) };
      return { ok: true, json: async () => ({ id: 3, title: "Track", lyric_query: "Track" }) };
    }
    if (uri.startsWith("/api/netease/preview"))
      return {
        ok: true,
        json: async () => ({ source_id: "7788", title: "Track", uploader: "Artist", duration: 90 })
      };
    if (uri.startsWith("/api/netease/search"))
      return {
        ok: true,
        json: async () => ({
          songs: [{ source_id: "7788", title: "Track", uploader: "Artist", duration: 90 }]
        })
      };
    return { ok: true, json: async () => ({ id: 5, title: "B Track", lyric_query: "B Track" }) };
  };
  const { setupSongRequests } = await import(url);
  let refreshes = 0;
  setupSongRequests({
    refresh: async () => {
      refreshes++;
    },
    openLyricPicker: async (...args) => pickers.push(args),
    videoPreview
  });
  return {
    byId,
    calls,
    pickers,
    choices,
    get previewClosed() {
      return previewClosed;
    },
    set pageCount(value) {
      pageCount = value;
    },
    get refreshes() {
      return refreshes;
    },
    set rejectOrder(value) {
      rejectOrder = value;
    },
    cleanup() {
      Object.assign(global, original);
    }
  };
}
const flush = async () => {
  for (let i = 0; i < 12; i++) await new Promise((resolve) => setImmediate(resolve));
};
async function search(h, query) {
  h.byId("netease-song-query").value = query;
  h.byId("netease-search-form").listeners.submit({ preventDefault() {} });
  await flush();
  return h.byId("netease-search-results").children[0];
}
test("NetEase successful song order clears the long search input and stale results", async () => {
  const h = await harness();
  try {
    const item = await search(h, "https://music.163.com/song?id=7788&long=" + "a".repeat(700));
    assert.ok(item);
    assert.equal(h.byId("netease-search-results").children.length, 1);
    await item.children.at(-1).onclick();
    assert.equal(h.byId("netease-song-query").value, "");
    assert.equal(h.byId("netease-search-results").children.length, 0);
    assert.equal(h.byId("netease-search-message").textContent, "");
    assert.equal(h.refreshes, 1);
    assert.deepEqual(h.pickers, [[3, "Track", "Track"]]);
  } finally {
    h.cleanup();
  }
});
test("NetEase rejected order preserves search input and results to retry", async () => {
  const h = await harness();
  try {
    const item = await search(h, "Track");
    h.rejectOrder = true;
    await item.children.at(-1).onclick();
    assert.equal(h.byId("netease-song-query").value, "Track");
    assert.equal(h.byId("netease-search-results").children.length, 1);
    assert.equal(h.refreshes, 0);
    assert.equal(h.pickers.length, 0);
  } finally {
    h.cleanup();
  }
});
test("Bilibili single-P request goes straight to lyric picker", async () => {
  const h = await harness();
  try {
    h.byId("bvid").value = "BV1234567890";
    await h.byId("quick-submit-btn").onclick();
    assert.equal(h.byId("bvid").value, "");
    assert.equal(h.choices.length, 0);
    assert.equal(h.refreshes, 1);
    assert.equal(h.calls.filter(({ uri }) => uri === "/api/songs").length, 1);
    assert.deepEqual(h.pickers, [
      [
        5,
        "B Track",
        "B Track",
        {
          bvid: "BV1234567890",
          page: 1,
          title: "B Track"
        }
      ]
    ]);
  } finally {
    h.cleanup();
  }
});

test("Bilibili multi-P waits for selection and respects the link's ?p", async () => {
  const h = await harness();
  try {
    h.pageCount = 2;
    h.byId("bvid").value = "https://www.bilibili.com/video/BV1pX4y1a7Cd/?p=2";
    await h.byId("quick-submit-btn").onclick();
    assert.equal(h.choices.length, 1);
    assert.equal(h.choices[0].page, 2);
    assert.equal(h.choices[0].info.bvid, "BV1pX4y1a7Cd");
    assert.equal(h.calls.filter(({ uri }) => uri === "/api/songs").length, 0);
    assert.equal(h.byId("bvid").value, "https://www.bilibili.com/video/BV1pX4y1a7Cd/?p=2");
    await h.choices[0].submit(2);
    const submitted = h.calls.filter(({ uri }) => uri === "/api/songs");
    assert.equal(submitted.length, 1);
    assert.deepEqual(JSON.parse(submitted[0].opts.body), { bvid: "BV1pX4y1a7Cd", page: 2 });
    assert.equal(h.byId("bvid").value, "");
    assert.equal(h.previewClosed, 1);
    assert.equal(h.pickers[0][3].page, 2);
  } finally {
    h.cleanup();
  }
});

test("Bilibili failed multi-P order retains the input and selection", async () => {
  const h = await harness();
  try {
    h.pageCount = 2;
    h.byId("bvid").value = "BV1pX4y1a7Cd";
    await h.byId("quick-submit-btn").onclick();
    h.rejectOrder = true;
    await assert.rejects(h.choices[0].submit(2), /submission failed/);
    assert.equal(h.byId("bvid").value, "BV1pX4y1a7Cd");
    assert.equal(h.previewClosed, 0);
    assert.equal(h.pickers.length, 0);
  } finally {
    h.cleanup();
  }
});
