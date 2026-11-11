"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const moduleUrl = pathToFileURL(path.join(__dirname, "../static/admin-settings.js")).href;

async function fixture() {
  const oldDocument = global.document;
  const oldFetch = global.fetch;
  const oldTimeout = global.setTimeout;
  const oldClearTimeout = global.clearTimeout;
  const original = {
    queue_policy: "fair",
    subtitle_style: "classic",
    video_quality: 1080,
    allow_unsubtitled: false,
    max_bandwidth_mbps: 3,
    orders_paused: false,
    tv_html_overlay: true,
    original_codec_mode: "av1"
  };
  let saved = structuredClone(original);
  let failNextSave = false;
  let refreshCount = 0;
  const calls = [];
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        type: ["allow-unsubtitled", "pause-orders", "tv-html-overlay"].includes(id)
          ? "checkbox"
          : "select-one",
        value: "",
        checked: false,
        disabled: false,
        textContent: "",
        className: "",
        events: {},
        addEventListener(name, callback) {
          this.events[name] = callback;
        },
        setAttribute() {}
      });
    }
    return elements.get(id);
  };
  global.document = { getElementById: get, activeElement: null };
  global.setTimeout = () => 1;
  global.clearTimeout = () => {};
  global.fetch = async (url, opts = {}) => {
    const method = opts.method || "GET";
    calls.push([method, url]);
    if (method === "PUT") {
      if (failNextSave) {
        failNextSave = false;
        return { ok: false, status: 503, json: async () => ({ detail: "temporarily down" }) };
      }
      Object.assign(saved, JSON.parse(opts.body));
    }
    return { ok: true, json: async () => structuredClone(saved) };
  };
  const { setupAdminSettings } = await import(moduleUrl);
  const settings = setupAdminSettings({
    refresh: async () => {
      refreshCount++;
    },
    invalidateQueueState: () => {}
  });
  return {
    ...settings,
    get,
    calls,
    setServer(values) {
      Object.assign(saved, values);
    },
    failSave() {
      failNextSave = true;
    },
    get refreshCount() {
      return refreshCount;
    },
    edit(id, value) {
      const item = get(id);
      if (item.type === "checkbox") item.checked = value;
      else item.value = String(value);
      item.events.change();
    },
    cleanup() {
      global.document = oldDocument;
      global.fetch = oldFetch;
      global.setTimeout = oldTimeout;
      global.clearTimeout = oldClearTimeout;
    }
  };
}

test("unsaved select, checkbox and numeric settings persist across blur and refresh", async () => {
  const h = await fixture();
  try {
    await h.refreshAdminSettings("first");
    h.edit("queue-policy", "fcfs");
    h.edit("pause-orders", true);
    h.edit("max-bandwidth-mbps", "4");
    h.setServer({ video_quality: 720, max_bandwidth_mbps: 2 });
    await h.refreshAdminSettings("first");
    assert.equal(h.get("queue-policy").value, "fcfs");
    assert.equal(h.get("pause-orders").checked, true);
    assert.equal(h.get("max-bandwidth-mbps").value, "4");
    assert.equal(h.get("video-quality").value, "720", "clean fields should update");
    await h.get("queue-settings-save").onclick();
    await h.refreshAdminSettings("first");
    assert.equal(h.get("queue-policy").value, "fcfs");
    h.setServer({ queue_policy: "fair", orders_paused: false });
    await h.refreshAdminSettings("first");
    assert.equal(h.get("queue-policy").value, "fair", "saved fields are no longer dirty");
    assert.equal(h.get("pause-orders").checked, false);
    assert.equal(h.get("max-bandwidth-mbps").value, "4", "other form remains dirty");
  } finally {
    h.cleanup();
  }
});

test("failed save preserves draft, and event switch resets it", async () => {
  const h = await fixture();
  try {
    await h.refreshAdminSettings("first");
    h.edit("original-codec-mode", "h264");
    h.failSave();
    await h.get("settings-save-btn").onclick();
    await h.refreshAdminSettings("first");
    assert.equal(h.get("original-codec-mode").value, "h264");
    assert.equal(h.get("toast").textContent, "temporarily down");
    h.setServer({ original_codec_mode: "default" });
    await h.refreshAdminSettings("second");
    assert.equal(h.get("original-codec-mode").value, "default");
    h.edit("original-codec-mode", "h264");
    await h.refreshAdminSettings(null);
    await h.refreshAdminSettings("third");
    assert.equal(h.get("original-codec-mode").value, "default");
  } finally {
    h.cleanup();
  }
});
