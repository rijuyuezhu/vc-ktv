"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function loadUI() {
  // Common UI primitives are native browser ES modules; test without a bundler.
  const source = fs.readFileSync(path.join(__dirname, "../static/ui-common.js"));
  return import("data:text/javascript;base64," + source.toString("base64"));
}

test("shared UI helpers preserve API, button and DOM semantics", async () => {
  const { api, guarded, duration, appendText, toast, error, eventRow, actionButton, emptyState } =
    await loadUI();
  const before = {
    fetch: global.fetch,
    document: global.document,
    setTimeout: global.setTimeout,
    clearTimeout: global.clearTimeout
  };
  const calls = [];
  const attrs = {};
  const toastElement = {
    textContent: "",
    className: "",
    setAttribute(key, val) {
      attrs[key] = val;
    }
  };
  const created = [];
  global.fetch = async (url, options) => {
    calls.push([url, options]);
    if (url === "/error")
      return { ok: false, status: 409, json: async () => ({ detail: "歌曲已切换" }) };
    return { ok: true, json: async () => ({ ok: true }) };
  };
  const eventTemplate = {
    content: {
      firstElementChild: {
        cloneNode: () => {
          const title = { textContent: "" };
          const metadata = { textContent: "" };
          const chevron = { textContent: "›", attrs: { "aria-hidden": "true" } };
          return {
            type: "button",
            className: "song-item archive-entry",
            children: [title, metadata, chevron],
            querySelector: (selector) => (selector === ".event-title" ? title : metadata)
          };
        }
      }
    }
  };
  global.document = {
    getElementById: (id) =>
      id === "event-row-template"
        ? eventTemplate
        : id === "empty-state-template"
          ? {
              content: {
                firstElementChild: { cloneNode: () => ({ className: "empty", role: "status" }) }
              }
            }
          : toastElement,
    createElement: (tag) => {
      const el = {
        tag,
        textContent: "",
        className: "",
        disabled: false,
        children: [],
        attrs: {},
        append(child) {
          this.children.push(child);
        },
        setAttribute(name, value) {
          this.attrs[name] = value;
        }
      };
      created.push(el);
      return el;
    }
  };
  global.setTimeout = (_, delay) => {
    assert.ok([4800, 12000, 30000].includes(delay), `unexpected timeout ${delay}`);
    return 7;
  };
  global.clearTimeout = () => {};
  try {
    assert.equal(duration(61.4), "1:01");
    assert.equal(duration(0), "0:00");
    assert.deepEqual(await api("/health"), { ok: true });
    assert.deepEqual(await api("/api/songs", "POST", { ref: "42" }), { ok: true });
    assert.equal(calls[0][1].credentials, "same-origin");
    assert.equal(calls[0][1].cache, "no-store");
    assert.deepEqual(JSON.parse(calls[1][1].body), { ref: "42" });
    assert.equal(calls[1][1].headers["Content-Type"], "application/json");
    await assert.rejects(api("/error"), /歌曲已切换/);
    const parent = {
      children: [],
      append(node) {
        this.children.push(node);
      }
    };
    const child = appendText(parent, "span", "歌曲", "title");
    assert.equal(child.textContent, "歌曲");
    assert.equal(child.className, "title");
    assert.equal(parent.children[0], child);
    assert.equal(created[0].tag, "span");
    let opened = false;
    const event = eventRow(parent, "测试场次", "公开", () => {
      opened = true;
    });
    assert.equal(event.type, "button");
    assert.equal(event.className, "song-item archive-entry");
    assert.deepEqual(
      event.children.map((item) => item.textContent),
      ["测试场次", "公开", "›"]
    );
    assert.equal(event.children[2].attrs["aria-hidden"], "true");
    event.onclick();
    assert.equal(opened, true);
    const empty = emptyState(parent, "暂无歌曲");
    assert.equal(empty.className, "empty");
    assert.equal(empty.textContent, "暂无歌曲");
    assert.equal(parent.children.at(-1), empty);
    let actionCount = 0;
    const sharedButton = actionButton(
      parent,
      "确认",
      async (self) => {
        assert.equal(self.disabled, true);
        actionCount++;
      },
      "subtle"
    );
    assert.equal(sharedButton.className, "btn subtle");
    await sharedButton.onclick();
    assert.equal(actionCount, 1);
    assert.equal(sharedButton.disabled, false);
    const button = { disabled: false };
    assert.equal(await guarded(async () => "ok", button), "ok");
    assert.equal(button.disabled, false);
    toast("完成");
    assert.equal(toastElement.textContent, "完成");
    assert.equal(toastElement.className, "show");
    assert.equal(attrs.role, "status");
    assert.equal(attrs["aria-live"], "polite");
    error(new Error("失败"));
    assert.equal(toastElement.className, "show error");
    assert.equal(toastElement.textContent, "失败");
    assert.equal(attrs.role, "alert");
    assert.equal(attrs["aria-live"], "assertive");
  } finally {
    Object.assign(global, before);
  }
});

test("hung reads and writes time out, releasing guarded controls without retrying", async () => {
  const { api, guarded } = await loadUI();
  const previous = {
    fetch: global.fetch,
    setTimeout: global.setTimeout,
    clearTimeout: global.clearTimeout,
    document: global.document
  };
  const calls = [];
  const toastNode = { textContent: "", setAttribute() {}, className: "" };
  global.document = { getElementById: () => toastNode };
  global.setTimeout = (fn, delay) => {
    if (delay === 12000 || delay === 30000) queueMicrotask(fn);
    return 1;
  };
  global.clearTimeout = () => {};
  global.fetch = async (url, opts) => {
    calls.push([url, opts.method]);
    return new Promise((_, reject) => {
      opts.signal.addEventListener(
        "abort",
        () => reject(new DOMException("stalled", "AbortError")),
        {
          once: true
        }
      );
    });
  };
  try {
    await assert.rejects(api("/hung"), /网络响应超时/);
    const button = { disabled: false };
    await guarded(() => api("/commit", "POST", { value: 1 }), button);
    assert.equal(button.disabled, false);
    assert.match(toastNode.textContent, /服务器可能已执行操作/);
    assert.deepEqual(calls, [
      ["/hung", "GET"],
      ["/commit", "POST"]
    ]);
  } finally {
    Object.assign(global, previous);
  }
});

test("lyrics may use an explicit 20s GET deadline without extending other API calls", async () => {
  const { api } = await loadUI();
  const previous = {
    fetch: global.fetch,
    setTimeout: global.setTimeout,
    clearTimeout: global.clearTimeout
  };
  const delays = [];
  global.setTimeout = (_, delay) => {
    delays.push(delay);
    return 1;
  };
  global.clearTimeout = () => {};
  global.fetch = async () => ({ ok: true, json: async () => ({ candidates: [] }) });
  try {
    await api("/api/songs/1/lyrics/candidates", "GET", null, new AbortController().signal, 20000);
    await api("/api/state");
    assert.deepEqual(delays, [20000, 12000]);
  } finally {
    Object.assign(global, previous);
  }
});
