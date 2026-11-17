"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { test } = require("node:test");

const url = pathToFileURL(path.join(__dirname, "../static/guest-remote.js")).href;

function element(tag = "div") {
  const attrs = new Map();
  const el = {
    tagName: tag.toUpperCase(),
    parentElement: null,
    children: [],
    className: "",
    textContent: "",
    src: "",
    title: "",
    disabled: false
  };
  el.append = (...children) => {
    for (const child of children) {
      if (child.parentElement) child.remove();
      child.parentElement = el;
      el.children.push(child);
    }
  };
  el.remove = () => {
    if (el.parentElement) {
      const siblings = el.parentElement.children;
      siblings.splice(siblings.indexOf(el), 1);
      el.parentElement = null;
    }
  };
  el.querySelector = (selector) =>
    selector === "img" ? el.children.find((child) => child.tagName === "IMG") : null;
  el.setAttribute = (name, value) => attrs.set(name, value);
  el.getAttribute = (name) => attrs.get(name);
  el.classList = {
    contains(name) {
      return el.className.split(" ").includes(name);
    },
    toggle(name, force) {
      const current = new Set(el.className.split(" ").filter(Boolean));
      const visible = force ?? !current.has(name);
      if (visible) current.add(name);
      else current.delete(name);
      el.className = [...current].join(" ");
    }
  };
  return el;
}

async function setup({ confirm = true, api = async () => ({ seq: 1 }) } = {}) {
  const prior = global.document;
  global.document = { createElement: element };
  const { setupGuestRemote } = await import(url);
  const container = element();
  let refreshed = 0;
  let confirmed = 0;
  const calls = [];
  const component = setupGuestRemote({
    container,
    api: (...args) => {
      calls.push(args);
      return api(...args);
    },
    refresh: () => refreshed++,
    confirmSkip: () => {
      confirmed++;
      return confirm;
    }
  });
  const own = { id: 12, status: "playing", mine: true };
  const remote = {
    available: true,
    song_id: 12,
    online: true,
    paused: false,
    pending: false,
    audio_mode: "original",
    backing_ready: false
  };
  return {
    own,
    remote,
    container,
    component,
    calls,
    buttons() {
      return container.children[0]?.children[0]?.children;
    },
    message() {
      return container.children[0]?.children[1]?.textContent;
    },
    refreshed: () => refreshed,
    confirmed: () => confirmed,
    cleanup: () => {
      global.document = prior;
    }
  };
}

test("guest controller appears only on the requester's live song", async () => {
  const h = await setup();
  try {
    const { own, remote, component, container } = h;
    component.render({ ...own, mine: false }, remote);
    assert.equal(container.children.length, 0);
    component.render({ ...own, status: "played" }, remote);
    assert.equal(container.children.length, 0);
    component.render(own, { ...remote, available: false });
    assert.equal(container.children.length, 0);
    component.render(own, { ...remote, song_id: 13 });
    assert.equal(container.children.length, 0);
    component.render(own, remote);
    assert.equal(container.children.length, 1);
    assert.equal(h.buttons().length, 4);
    assert.equal(
      h.buttons()[0].classList.contains("hidden"),
      true,
      "no silent backing button before ready"
    );
    assert.equal(h.buttons()[1].title, "暂停");
    component.render(own, { ...remote, online: false });
    assert.equal(h.buttons()[1].disabled, true);
    assert.equal(h.message(), "主屏离线");
    component.render(own, { ...remote, pending: true });
    assert.equal(h.buttons()[2].disabled, true);
    assert.equal(h.message(), "等待主屏确认…");
    component.render({ id: 13, status: "playing", mine: true }, { ...remote, song_id: 13 });
    assert.equal(h.message(), "");
    component.reset();
    assert.equal(container.children.length, 0);
  } finally {
    h.cleanup();
  }
});

test("backing switch uses same guest endpoint, pause/resume and restart share active song", async () => {
  const h = await setup();
  try {
    const ready = { ...h.remote, backing_ready: true };
    h.component.render(h.own, ready);
    assert.equal(h.buttons()[0].textContent, "伴唱");
    assert.equal(h.buttons()[0].disabled, false);
    await h.buttons()[0].onclick();
    assert.deepEqual(h.calls.at(-1), [
      "/api/guest/tv-remote",
      "POST",
      {
        action: "audio_mode",
        song_id: 12,
        value: "backing"
      }
    ]);
    assert.equal(h.refreshed(), 1);
    h.component.render(h.own, { ...ready, audio_mode: "backing" });
    assert.equal(h.buttons()[0].textContent, "原唱");
    await h.buttons()[0].onclick();
    assert.equal(h.calls.at(-1)[2].value, "original");
    h.component.render(h.own, { ...ready, paused: true, audio_mode: "original" });
    await h.buttons()[1].onclick();
    assert.equal(h.calls.at(-1)[2].action, "resume");
    h.component.render(h.own, ready);
    await h.buttons()[1].onclick();
    assert.equal(h.calls.at(-1)[2].action, "pause");
    await h.buttons()[2].onclick();
    assert.equal(h.calls.at(-1)[2].action, "restart");
  } finally {
    h.cleanup();
  }
});

test("double taps are single flight and an admin-pending command disables controls", async () => {
  let finish;
  const h = await setup({
    api: () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  });
  try {
    h.component.render(h.own, h.remote);
    const first = h.buttons()[1].onclick();
    await h.buttons()[1].onclick();
    assert.equal(h.calls.length, 1);
    assert.equal(h.buttons()[2].disabled, true);
    finish({ seq: 8 });
    await first;
    assert.equal(h.refreshed(), 1);
    h.component.render(h.own, { ...h.remote, pending: true });
    assert.equal(h.buttons()[1].disabled, true);
    await h.buttons()[1].onclick();
    assert.equal(h.calls.length, 1);
  } finally {
    h.cleanup();
  }
});

test("old HTTP replies cannot restore the controller after song handoff", async () => {
  let finish;
  const h = await setup({
    api: () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  });
  try {
    h.component.render(h.own, h.remote);
    const first = h.buttons()[2].onclick();
    h.component.render({ ...h.own, id: 13 }, { ...h.remote, song_id: 13 });
    assert.equal(h.message(), "");
    finish({ seq: 4 });
    await first;
    assert.equal(h.message(), "");
    h.component.render(null, null);
    assert.equal(h.container.children.length, 0);
  } finally {
    h.cleanup();
  }
});

test("skip requires explicit confirmation and a rejected request reports its error", async () => {
  const denied = await setup({ confirm: false });
  try {
    denied.component.render(denied.own, denied.remote);
    await denied.buttons()[3].onclick();
    assert.equal(denied.confirmed(), 1);
    assert.equal(denied.calls.length, 0);
  } finally {
    denied.cleanup();
  }
  const failed = await setup({
    api: async () => {
      throw Error("歌曲已切换");
    }
  });
  try {
    failed.component.render(failed.own, failed.remote);
    await failed.buttons()[3].onclick();
    assert.equal(failed.calls.length, 1);
    assert.equal(failed.message(), "歌曲已切换");
    assert.equal(failed.refreshed(), 1);
  } finally {
    failed.cleanup();
  }
});
