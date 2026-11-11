import { $, api, toast, guarded, appendText } from "./ui-common.js";

// Both entry points use the same routed archive list and song-detail surfaces.
export function setupArchiveNavigation({
  compactSong,
  renderArchivedSong,
  openLyricPicker,
  refresh,
  navigate,
  getArchiveOrigin,
  canRequestSong,
  canManageArchives
}) {
  const eventPattern = /^archive-event-([A-Za-z0-9_-]{1,60})$/;
  const songPattern = /^archive-song-([A-Za-z0-9_-]{1,60})-([0-9]+)$/;
  const saved = new Map();
  let searchTerm = "";
  let loadedRoute = null;
  let loadVersion = 0;

  function parse(route) {
    if (route === "archive-events") return { page: "archive-events" };
    const song = songPattern.exec(route);
    if (song) return { page: "archive-song", eventId: song[1], songId: Number(song[2]) };
    const event = eventPattern.exec(route);
    if (event) return { page: "archive-event", eventId: event[1] };
    return null;
  }
  function title(route) {
    const data = parse(route);
    return data?.page === "archive-events"
      ? "其它场次"
      : data?.page === "archive-event"
        ? "场次歌单"
        : "歌曲详情";
  }
  function parent(route) {
    const data = parse(route);
    if (!data) return "#";
    if (data.page === "archive-song") return "#archive-event-" + data.eventId;
    if (data.page === "archive-event")
      return getArchiveOrigin() === "settings-history" ? "#settings-history" : "#archive-events";
    return getArchiveOrigin() === "played" ? "#played" : "#";
  }
  function clear() {
    loadedRoute = null;
    loadVersion++;
    saved.clear();
  }
  function eventLink(row, event, origin) {
    row.onclick = () => navigate("archive-event-" + event.id, origin);
  }
  function renderEvents(data) {
    const panel = $("page-archive-events");
    const key = JSON.stringify(data.events);
    if (saved.get("archive-events") === key && panel.children.length) return;
    saved.set("archive-events", key);
    panel.replaceChildren();
    const section = appendText(panel, "section", "", "panel");
    const input = appendText(section, "input", "");
    input.type = "search";
    input.placeholder = "搜索场次";
    input.setAttribute("aria-label", "搜索公开历史场次");
    input.value = searchTerm;
    const list = appendText(section, "div", "", "archive-list");
    const draw = () => {
      list.replaceChildren();
      const events = data.events.filter((e) =>
        e.name.toLowerCase().includes(searchTerm.toLowerCase())
      );
      if (!events.length) appendText(list, "p", "暂无匹配场次", "empty");
      for (const event of events) {
        const row = appendText(list, "button", "", "song-item archive-entry");
        row.type = "button";
        appendText(row, "strong", event.name);
        appendText(row, "small", (event.created_at || "").slice(0, 10));
        appendText(row, "span", "›", "song-chevron");
        eventLink(row, event, "played");
      }
    };
    input.oninput = () => {
      searchTerm = input.value;
      draw();
    };
    draw();
  }
  function adminButton(row, label, action, dangerous = false) {
    const btn = appendText(row, "button", label, "btn " + (dangerous ? "subtle" : "neutral"));
    btn.type = "button";
    btn.onclick = () => guarded(() => action(), btn);
    return btn;
  }
  function renderEvent(data) {
    const event = data.event;
    const key = "archive-event-" + event.id;
    const target = $("page-archive-event");
    const signature = JSON.stringify(data);
    if (
      saved.get(key) === signature &&
      target.dataset.eventId === event.id &&
      target.children.length
    )
      return;
    saved.set(key, signature);
    target.dataset.eventId = event.id;
    target.replaceChildren();
    const section = appendText(target, "section", "", "panel");
    const head = appendText(section, "div", "", "section-head");
    appendText(head, "h2", event.name);
    const status = event.media_released_at ? "已释放资源" : "已散场";
    appendText(
      section,
      "p",
      [(event.created_at || "").slice(0, 10), status].filter(Boolean).join("　"),
      "muted"
    );
    if (canManageArchives() && !event.legacy) {
      const actions = appendText(section, "div", "", "detail-tools archive-event-tools");
      const csv = appendText(actions, "a", "↓ CSV", "btn neutral");
      csv.href = "/api/admin/events/" + encodeURIComponent(event.id) + "/songs.csv";
      const visibility = adminButton(actions, event.public ? "公开 ✓" : "私有", async () => {
        await api(
          "/api/admin/events/" +
            encodeURIComponent(event.id) +
            "/visibility?public=" +
            String(!event.public),
          "PUT"
        );
        toast(event.public ? "已设为私有" : "已设为公开");
        clear();
        await refresh();
      });
      visibility.title = "切换场次可见性";
      if (!event.media_released_at)
        adminButton(actions, "释放资源", async () => {
          if (!confirm("删除本场视频等大文件？保留歌词和记录。")) return;
          const result = await api(
            "/api/admin/events/" + encodeURIComponent(event.id) + "/release-media",
            "POST"
          );
          toast("已释放 " + (result.freed_bytes / 1048576).toFixed(1) + " MiB");
          clear();
          await refresh();
        });
      adminButton(
        actions,
        "删除记录",
        async () => {
          if (prompt("输入场次名称确认删除：") !== event.name) return;
          await api("/api/admin/events/" + encodeURIComponent(event.id), "DELETE");
          toast("已删除场次");
          clear();
          navigate("settings-history");
        },
        true
      );
    } else if (canManageArchives()) {
      const csv = appendText(section, "a", "↓ CSV", "btn neutral");
      csv.href = "/api/admin/events/" + encodeURIComponent(event.id) + "/songs.csv";
    }
    const list = appendText(section, "div", "", "song-list archive-song-list");
    if (!data.songs.length) appendText(list, "p", "暂无歌曲", "empty");
    data.songs.forEach((song, i) => {
      list.append(
        compactSong({ ...song, nickname: song.requester }, i + 1, () =>
          navigate("archive-song-" + event.id + "-" + song.id, getArchiveOrigin())
        )
      );
    });
  }
  function renderSong(data) {
    const { song, event } = data;
    const key = "archive-song-" + event.id + "-" + song.id;
    const target = $("page-archive-song");
    const signature = JSON.stringify(data);
    if (saved.get(key) === signature && target.dataset.songKey === key && target.children.length)
      return;
    saved.set(key, signature);
    target.dataset.songKey = key;
    const panel = renderArchivedSong(target, song);
    if (canManageArchives() && !event.legacy) {
      const section = appendText(panel, "section", "", "detail-section");
      appendText(section, "h3", "记录状态");
      const controls = appendText(section, "div", "", "detail-tools");
      const select = appendText(controls, "select", "");
      select.setAttribute("aria-label", "历史歌曲状态");
      select.className = "archive-status-select";
      for (const [value, text] of [
        ["played", "已唱"],
        ["skipped", "已跳过"],
        ["cancelled", "已取消"],
        ["closed_unplayed", "散场未唱"],
        ["closed_interrupted", "散场中断"]
      ]) {
        const option = appendText(select, "option", text);
        option.value = value;
      }
      select.value = song.status;
      select.onchange = () =>
        guarded(async () => {
          try {
            await api(
              `/api/admin/events/${encodeURIComponent(event.id)}/songs/${song.id}/status?status=${select.value}`,
              "PUT"
            );
          } catch (e) {
            select.value = song.status;
            throw e;
          }
          toast("状态已更新");
          saved.delete("archive-event-" + event.id);
          saved.delete(key);
          loadedRoute = null;
          await refresh();
        }, select);
    }
    const section = appendText(panel, "section", "", "detail-section");
    appendText(section, "h3", "歌曲操作");
    const row = appendText(section, "div", "", "detail-tools");
    const repeat = adminButton(row, "↻ 再点一首", async () => {
      const info =
        song.source_type === "netease"
          ? song.source_id
            ? await api("/api/netease/songs", "POST", { ref: String(song.source_id) })
            : null
          : song.bvid
            ? await api("/api/songs", "POST", { bvid: song.bvid, page: song.page || 1 })
            : null;
      if (!info) throw Error("这首歌没有可用的来源");
      toast("已加入队列，请选择歌词");
      await refresh();
      await openLyricPicker(
        info.id,
        info.title,
        info.lyric_query,
        song.bvid ? { bvid: song.bvid, page: song.page || 1, title: song.title } : null
      );
    });
    repeat.title = canRequestSong() ? "在当前场次重新点歌" : "请先加入当前场次";
    if (!(song.source_type === "netease" ? song.source_id : song.bvid) || !canRequestSong())
      repeat.disabled = true;
  }
  async function load(force = false) {
    const route = window.location.hash.replace(/^#\/?/, "");
    const parsed = parse(route);
    if (!parsed || (!force && route === loadedRoute)) return;
    loadedRoute = route;
    const version = ++loadVersion;
    try {
      const data = await api(
        parsed.page === "archive-events"
          ? "/api/events"
          : parsed.page === "archive-event"
            ? "/api/events/" + encodeURIComponent(parsed.eventId) + "/songs"
            : "/api/events/" + encodeURIComponent(parsed.eventId) + "/songs/" + parsed.songId
      );
      if (version !== loadVersion || window.location.hash.replace(/^#\/?/, "") !== route) return;
      if (parsed.page === "archive-events") renderEvents(data);
      else if (parsed.page === "archive-event") renderEvent(data);
      else renderSong(data);
    } catch (e) {
      if (version !== loadVersion || window.location.hash.replace(/^#\/?/, "") !== route) return;
      loadedRoute = null;
      $("page-" + parsed.page).replaceChildren();
      appendText($("page-" + parsed.page), "p", e.message || "无法打开历史记录", "empty");
    }
  }
  return { parse, title, parent, load, clear };
}
