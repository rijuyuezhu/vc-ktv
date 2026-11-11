import { setupAdminPlayback } from "./admin-playback.js";
import { setupSongDetail } from "./song-detail.js";
import { setupArchiveNavigation } from "./archive-navigation.js";
import { setupAdminEvents } from "./admin-events.js";
import { setupSongRequests, setupBilibiliPreview } from "./song-requests.js";
import { createLyricPicker } from "./lyrics.js";
import { $, api, toast, error, guarded, duration, appendText, emptyState } from "./ui-common.js";
("use strict");
let admin = false;
let adminRole = null;
let adminCanOrder = false;
let refreshing = false;
let refreshQueued = false;
let refreshFailed = false;
let guestStateKey = null;
let adminStateKey = null;
const listKeys = new Map();
let currentEventId = null;
let guestAuthenticated = false;
let playedKey = null;

const isAdminPath = () => window.location.pathname.replace(/\/$/, "") === "/admin";

async function cancelGuestSong(songId) {
  if (!confirm("取消自己点的这首歌？")) return;
  await api(`/api/songs/${songId}/cancel`, "POST");
  if (lyricPicker.selectedSongId() === songId && $("guest-lyrics-dialog").open)
    $("guest-lyrics-dialog").close();
  if (currentRoute() === "song-" + songId) window.location.hash = "";
  toast("✓ 已取消点歌");
  await refresh();
}
function songState(song) {
  if (song.status === "playing") return "播放中";
  if (song.status === "played") return "已唱";
  if (song.status === "skipped") return "已跳过";
  if (song.status === "cancelled") return "已取消";
  if (song.status === "closed_unplayed") return "散场未唱";
  if (song.status === "closed_interrupted") return "散场中断";
  if (song.status === "rendering") return "制作中";
  if (song.prep_state === "unavailable") return "下载失败";
  if (song.prep_state === "original" && !song.skip_subtitles) return "字幕失败";
  if (song.status === "ready" && !song.awaiting_subtitles) return "待播";
  if (song.prep_state === "downloaded" && !song.lyric_selected && !song.skip_subtitles)
    return "待选歌词";
  if (["aligning", "downloading"].includes(song.prep_state)) return "制作中";
  return "准备中";
}
function addSourceTag(parent, song) {
  const isNetease = song.source_type === "netease";
  const tag = appendText(parent, "span", "", "source-tag");
  const image = appendText(tag, "img", "");
  image.src = isNetease ? "/icons/neteasecloudmusic.svg" : "/icons/bilibili.svg";
  image.alt = "";
  image.setAttribute("aria-hidden", "true");
  appendText(tag, "span", isNetease ? "网易云" : "B站");
  tag.title = isNetease ? "网易云音乐" : "哔哩哔哩";
}
function compactSong(song, order, onOpen = null) {
  const row = $("song-row-template").content.firstElementChild.cloneNode(true);
  row.setAttribute(
    "aria-label",
    song.title + "，点歌者 " + (song.nickname || "未知") + "，" + songState(song) + "，查看详情"
  );
  row.querySelector(".ordinal").textContent = String(order);
  row.querySelector(".song-copy strong").textContent =
    song.title + (song.source_type !== "netease" && song.page > 1 ? " (P" + song.page + ")" : "");
  const meta = row.querySelector(".song-meta");
  meta.querySelector(".song-requester").textContent = song.nickname || "未知点歌者";
  meta.querySelector(".song-author").textContent = song.uploader || "未知作者";
  addSourceTag(meta, song);
  const status = row.querySelector(".song-state");
  status.textContent = songState(song);
  status.classList.toggle("failed", ["下载失败", "字幕失败"].includes(status.textContent));
  row.onclick = onOpen || (() => navigate("song-" + song.id));
  return row;
}
function fillList(id, songs) {
  const key = JSON.stringify(songs);
  if (listKeys.get(id) === key) return;
  listKeys.set(id, key);
  const target = $(id);
  target.replaceChildren();
  if (!songs.length) {
    emptyState(target, "暂无歌曲");
    return;
  }
  songs.forEach((s, i) => target.append(compactSong(s, i + 1)));
}
function renderCurrent(id, song) {
  const target = $(id);
  target.replaceChildren();
  appendText(target, "span", "正在播放", "current-label");
  if (song) target.append(compactSong(song, "▶"));
  else emptyState(target, "暂无正在播放的歌曲");
}
async function refreshSongHistory() {
  const data = await api("/api/songs/history" + (isAdminPath() ? "?admin_view=true" : ""));
  const key = JSON.stringify(data);
  if (playedKey === key) return;
  playedKey = key;
  const target = $("page-played");
  target.replaceChildren();
  if (!data.songs.length) {
    emptyState(target, "暂无歌曲记录");
    return;
  }
  const list = appendText(target, "div", "", "song-list");
  data.songs.forEach((song, index) => list.append(compactSong(song, index + 1)));
}
const videoPreview = setupBilibiliPreview();
const lyricPicker = createLyricPicker({ refresh, cancelGuestSong, videoPreview });
const { openLyricPicker } = lyricPicker;

setupSongRequests({ refresh, openLyricPicker, videoPreview });
const { refreshSongDetail, invalidateSongDetail, renderArchivedSong } = setupSongDetail({
  refresh,
  currentRoute,
  isAdminPath,
  songState,
  addSourceTag,
  openLyricPicker,
  cancelGuestSong
});

function currentRoute() {
  return window.location.hash.replace(/^#\/?/, "");
}
const scrollPositions = new Map();
let lastRoute = currentRoute();
let archiveOrigin = window.history.state?.archiveOrigin || "";
function navigate(route, origin) {
  scrollPositions.set(currentRoute(), window.scrollY);
  if (origin) archiveOrigin = origin;
  window.history.pushState(
    {
      archiveOrigin,
      returnTo: "#" + currentRoute()
    },
    "",
    "#" + route
  );
  window.dispatchEvent(new Event("hashchange"));
}
const archiveNav = setupArchiveNavigation({
  compactSong,
  renderArchivedSong,
  openLyricPicker,
  refresh,
  navigate,
  getArchiveOrigin: () => window.history.state?.archiveOrigin || archiveOrigin,
  canRequestSong: () => (isAdminPath() ? adminCanOrder : guestAuthenticated),
  canManageArchives: () => isAdminPath() && admin && adminRole === "global"
});
$("home-brand").href = isAdminPath() ? "/admin/" : "/";
$("home-brand").onclick = (event) => {
  event.preventDefault();
  window.location.hash = "";
};
$("subpage-action").onclick = (e) => {
  e.preventDefault();
  navigate("archive-events", "played");
};
const adminRoutes = {
  settings: ["设置与统计", null],
  "settings-playback": ["播放与视频", "event-settings-panel"],
  "settings-queue": ["点歌与队列", "event-queue-settings-panel"],
  "settings-event": ["场次管理", "event-management"],
  "settings-accounts": ["管理员账号", "admin-accounts-panel"],
  "settings-netease": ["网易云音乐连接", "netease-admin-panel"],
  "settings-history": ["历史歌单", "event-archive-panel"],
  stats: ["演唱统计", "performance-panel"]
};
function setupSubpages() {
  const root = $("subpage-content");
  const menu = document.createElement("nav");
  menu.className = "panel page-links";
  for (const [route, label, description] of [
    ["settings-playback", "播放与视频", "设备、字幕、画质"],
    ["settings-queue", "点歌与队列", "顺序、点歌权限"],
    ["settings-event", "场次管理", "加入码与场次"],
    ["settings-accounts", "管理员账号", "账号与权限"],
    ["settings-netease", "网易云音乐", "音乐源连接"],
    ["settings-history", "历史歌单", "记录与导出"],
    ["stats", "演唱统计", "各点歌者的演唱次数和时长"]
  ]) {
    const link = appendText(menu, "a", "", "settings-nav-item");
    link.href = "#" + route;
    const copy = appendText(link, "span", "", "settings-nav-copy");
    appendText(copy, "strong", label);
    appendText(copy, "small", description);
    appendText(link, "span", "›", "settings-nav-chevron").setAttribute("aria-hidden", "true");
  }
  const settings = document.createElement("div");
  settings.id = "page-settings";
  settings.append(menu);
  root.append(settings);
  for (const name of ["played", "song", "archive-events", "archive-event", "archive-song"]) {
    const wrapper = document.createElement("div");
    wrapper.id = "page-" + name;
    wrapper.className = "hidden";
    root.append(wrapper);
  }
  for (const [route, [, nodeId]] of Object.entries(adminRoutes)) {
    if (!nodeId) continue;
    const wrapper = document.createElement("div");
    wrapper.id = "page-" + route;
    wrapper.className = "hidden";
    const node = $(nodeId);
    if (node.tagName === "DETAILS") {
      node.open = true;
      node.classList.remove("fold-panel");
    }
    wrapper.append(node);
    root.append(wrapper);
  }
}
function showPage() {
  const isAdmin = isAdminPath();
  const route = currentRoute();
  const archivedPage = archiveNav.parse(route)?.page;
  const page =
    archivedPage ||
    (isAdmin && adminRoutes[route]
      ? route
      : route === "played"
        ? "played"
        : /^song-[0-9]+$/.test(route)
          ? "song"
          : "");
  const authenticated = isAdmin ? admin : guestAuthenticated;
  $("guest-view").classList.toggle("hidden", isAdmin || Boolean(page && authenticated));
  $("admin-view").classList.toggle("hidden", !isAdmin);
  $("admin-panel").classList.toggle("hidden", !admin || !!page);
  $("subpage").classList.toggle("hidden", !page || !authenticated);
  $("tab-guest").classList.toggle("selected", !isAdmin);
  $("tab-admin").classList.toggle("selected", isAdmin);
  $("tab-guest").setAttribute("aria-current", isAdmin ? "false" : "page");
  $("tab-admin").setAttribute("aria-current", isAdmin ? "page" : "false");
  $("hero-title").textContent = isAdmin ? "管理 KTV" : "想唱什么？";
  $("admin-settings-link").classList.toggle("hidden", !isAdmin || !admin);
  $("hero-title").closest(".hero").classList.toggle("hidden", !!page);
  $("subpage-action").classList.toggle("hidden", page !== "played");
  $("subpage-title").textContent = archivedPage
    ? archiveNav.title(route)
    : page === "played"
      ? "本场记录"
      : page === "song"
        ? "歌曲详情"
        : page
          ? adminRoutes[page][0]
          : "";
  $("subpage-back").href = archivedPage
    ? archiveNav.parent(route)
    : page === "song"
      ? window.history.state?.returnTo || "#"
      : page === "settings" || page === "played"
        ? "#"
        : "#settings";
  $("page-settings").classList.toggle("hidden", page !== "settings");
  if (page !== "settings-netease") {
    leaveNeteasePage();
  }
  for (const name of ["played", "song", "archive-events", "archive-event", "archive-song"])
    $("page-" + name).classList.toggle("hidden", page !== name);
  for (const routeName of Object.keys(adminRoutes)) {
    if (routeName !== "settings")
      $("page-" + routeName).classList.toggle("hidden", page !== routeName);
  }
  document.title =
    "VC-KTV | " +
    (archivedPage
      ? archiveNav.title(route)
      : page === "played"
        ? "本场记录"
        : page === "song"
          ? "歌曲详情"
          : page
            ? adminRoutes[page][0]
            : isAdmin
              ? "管理"
              : "点歌");
}
setupSubpages();
window.addEventListener("hashchange", () => {
  if (lastRoute !== currentRoute()) scrollPositions.set(lastRoute, window.scrollY);
  lastRoute = currentRoute();
  invalidateSongDetail();
  playedKey = null;
  showPage();
  refresh();
  requestAnimationFrame(() => window.scrollTo(0, scrollPositions.get(currentRoute()) || 0));
});
document.querySelectorAll(".password-field > input[type=password]").forEach((field) => {
  const button = $("password-toggle-template").content.firstElementChild.cloneNode(true);
  const label = field.id === "register-confirm" ? "重复密码" : "密码";
  button.setAttribute("aria-label", "显示" + label);
  field.after(button);
  button.onclick = () => {
    const revealing = field.type === "password";
    field.type = revealing ? "text" : "password";
    button.textContent = revealing ? "隐藏" : "显示";
    button.setAttribute("aria-pressed", String(revealing));
    button.setAttribute("aria-label", (revealing ? "隐藏" : "显示") + label);
    field.focus();
  };
});
$("show-user-login").onclick = () => {
  $("auth-register").classList.add("hidden");
  $("auth-login").classList.remove("hidden");
};
$("show-user-register").onclick = () => {
  $("auth-login").classList.add("hidden");
  $("auth-register").classList.remove("hidden");
};
$("login-box").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("/api/admin/login", "POST", {
      username: $("admin-username").value.trim() || null,
      password: $("password").value
    });
    $("password").value = "";
    admin = true;
    adminStateKey = null;
    toast("管理员登录成功");
    await refresh();
  }, $("login-btn"));
});
function updateAccount(signedIn, name) {
  const menu = $("account-menu");
  menu.classList.toggle("hidden", !signedIn);
  if (!signedIn) {
    menu.open = false;
    return;
  }
  $("account-name").textContent = name;
  $("account-name").title = name;
}
$("account-logout").onclick = () =>
  guarded(async () => {
    if (isAdminPath()) {
      if (!(await adminPlayback.prepareLogout())) return;
      await api("/api/admin/logout", "POST");
      admin = false;
      adminRole = null;
      adminCanOrder = false;
      adminStateKey = null;
      adminPlayback.resetPlayer();
    } else await api("/api/users/logout", "POST");
    archiveNav.clear();
    $("account-menu").open = false;
    updateAccount(false, "");
    await refresh();
  }, $("account-logout"));
const adminPlayback = setupAdminPlayback({
  refresh,
  isAdminPath,
  getAdmin: () => admin,
  addSourceTag,
  invalidateAdminCache: () => {
    adminStateKey = null;
  }
});
const { refreshEvents, leaveNeteasePage } = setupAdminEvents({
  refresh,
  currentRoute,
  updateAccount,
  onAdminRole: (role) => {
    if (adminRole !== role) {
      adminRole = role;
      archiveNav.clear();
    }
  },
  openArchivedEvent: (id) => navigate("archive-event-" + id, "settings-history"),
  onEventChanged: () => {
    invalidateSongDetail();
    playedKey = null;
  },
  invalidateQueueState: () => {
    adminStateKey = null;
    listKeys.clear();
  }
});

function renderEntry(entry) {
  if (entry.event?.id !== currentEventId) {
    invalidateSongDetail();
    playedKey = null;
    currentEventId = entry.event?.id || null;
    $("join-code").value = "";
    $("auth-login").classList.add("hidden");
    $("auth-register").classList.remove("hidden");
  }
  $("guest-entry-description").textContent = !entry.event
    ? "暂无活动场次"
    : entry.joined
      ? entry.event.name
      : entry.event.name + "  输入六位加入码";
  $("entry-code").classList.toggle("hidden", !entry.event || entry.joined);
  $("entry-account").classList.toggle(
    "hidden",
    !entry.event || !entry.joined || entry.authenticated
  );
  guestAuthenticated = Boolean(entry.authenticated);
  $("guest-access").classList.toggle("hidden", guestAuthenticated);
  if (!isAdminPath()) updateAccount(guestAuthenticated, entry.username || "");
  $("request-panel").classList.toggle("hidden", !entry.authenticated);
  $("queue-panel").classList.toggle("hidden", !entry.authenticated);
  showPage();
}
$("entry-code").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("/api/join", "POST", { code: $("join-code").value.trim() });
    $("join-code").value = "";
    await refresh();
  }, $("join-btn"));
});
$("register-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("/api/users/register", "POST", {
      username: $("register-name").value.trim(),
      password: $("register-password").value,
      confirm_password: $("register-confirm").value
    });
    $("register-password").value = "";
    $("register-confirm").value = "";
    toast("✓ 已进入");
    await refresh();
  }, $("register-btn"));
});
$("user-login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guarded(async () => {
    await api("/api/users/login", "POST", {
      username: $("user-login-name").value.trim(),
      password: $("user-login-password").value
    });
    $("user-login-password").value = "";
    toast("登录成功");
    await refresh();
  }, $("user-login-btn"));
});
async function refresh() {
  if (refreshing) {
    refreshQueued = true;
    return;
  }
  refreshing = true;
  try {
    const isGuest = !isAdminPath();
    if (isGuest) {
      const entry = await api("/api/entry");
      renderEntry(entry);
      if (entry.authenticated) {
        const state = await api("/api/state");
        const stateKey = JSON.stringify([
          entry.event?.id,
          state.current,
          state.queue,
          state.waiting
        ]);
        adminPlayback.updateRemoteControls();
        if (currentRoute() === "played") await refreshSongHistory();
        if (/^song-[0-9]+$/.test(currentRoute())) await refreshSongDetail();
        if (guestStateKey !== stateKey) {
          guestStateKey = stateKey;
          $("queue-count").textContent = state.queue.length + " 首可播";
          fillList("queue-ready", state.queue);
          fillList("queue-waiting", state.waiting);
          renderCurrent("current-guest", state.current);
        }
      } else {
        guestStateKey = null;
      }
    }
    if (isAdminPath()) {
      let adm;
      try {
        adm = await api("/api/admin/state");
        admin = true;
      } catch (e) {
        if (e.message.includes("管理员")) admin = false;
        else throw e;
      }
      $("login-box").classList.toggle("hidden", admin);
      $("admin-settings-link").classList.toggle("hidden", !admin);
      if (!admin) updateAccount(false, "");
      const route = currentRoute();
      const inSubpage = Boolean(
        adminRoutes[route] ||
        route === "played" ||
        archiveNav.parse(route) ||
        /^song-[0-9]+$/.test(route)
      );
      $("admin-panel").classList.toggle("hidden", !admin || inSubpage);
      $("subpage").classList.toggle("hidden", !admin || !inSubpage);
      if (admin) {
        await refreshEvents();
        const activeState = adm.active ? { ...adm.active, media_url: null } : null;
        const key = JSON.stringify([
          adm.waiting,
          adm.queue,
          adm.current,
          adm.guests,
          activeState,
          adm.playback_mode
        ]);
        const changed = key !== adminStateKey;
        await adminPlayback.syncFromAdminState(adm, changed);
        if (changed) {
          adminStateKey = key;
          $("admin-queue-count").textContent = adm.queue.length + " 首可播";
          renderCurrent("admin-current", adm.current);
          fillList("admin-queue-ready", adm.queue);
          fillList("admin-queue-waiting", adm.waiting);
          // All song operations live in the full-page song detail.

          const stats = $("stats");
          stats.replaceChildren();
          if (!adm.guests.length) emptyState(stats, "暂无演唱记录");
          adm.guests.forEach((u) => {
            const c = document.createElement("div");
            c.className = "stat";
            appendText(c, "strong", u.name);
            appendText(c, "small", u.plays + " 首  " + duration(u.seconds));
            stats.append(c);
          });
        }
        if (currentRoute() === "played") await refreshSongHistory();
        if (/^song-[0-9]+$/.test(currentRoute())) await refreshSongDetail();
      } else {
        adminStateKey = null;
        $("subpage").classList.add("hidden");
      }
    }
    if (archiveNav.parse(currentRoute()) && (isAdminPath() ? admin : guestAuthenticated)) {
      if (isAdminPath() && archiveNav.parse(currentRoute()).page === "archive-song") {
        const eligible = Boolean((await api("/api/entry")).authenticated);
        if (adminCanOrder !== eligible) {
          adminCanOrder = eligible;
          archiveNav.clear();
        }
      }
      await archiveNav.load();
    }
    if (refreshFailed) {
      toast("连接已恢复");
      refreshFailed = false;
    }
  } catch (e) {
    if (!refreshFailed) {
      error(e);
      refreshFailed = true;
    }
  } finally {
    refreshing = false;
    if (refreshQueued) {
      refreshQueued = false;
      refresh();
    }
  }
}
showPage();
adminPlayback.updatePlaybackUI();
refresh();
setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, 3500);
// Remote feedback is cheap and visible only while the TV controls are in use.
setInterval(adminPlayback.pollTVRemoteStatus, 350);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});
window.addEventListener("online", () => refresh());
