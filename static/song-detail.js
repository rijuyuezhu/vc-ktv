import { $, api, toast, guarded, duration, appendText } from "./ui-common.js";

// Song detail rendering, guest/admin actions and manual lyric editor.
export function setupSongDetail({
  refresh,
  currentRoute,
  isAdminPath,
  songState,
  addSourceTag,
  openLyricPicker,
  cancelGuestSong
}) {
  let detailKey = null;
  let popupSongId = null;
  let leadSeconds = 1.8;

  function invalidateSongDetail() {
    detailKey = null;
  }

  function sourceUrl(song) {
    return song.source_type === "netease"
      ? song.source_id
        ? "https://music.163.com/#/song?id=" + encodeURIComponent(song.source_id)
        : ""
      : song.bvid
        ? "https://www.bilibili.com/video/" +
          encodeURIComponent(song.bvid) +
          "/?p=" +
          (song.page || 1)
        : "";
  }
  async function copyValue(value, label) {
    if (!value) return;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
      else {
        const field = document.createElement("textarea");
        field.value = value;
        field.style.cssText = "position:fixed;left:-9999px";
        document.body.append(field);
        field.select();
        try {
          if (!document.execCommand("copy")) throw Error("clipboard");
        } finally {
          field.remove();
        }
      }
      toast("已复制" + label);
    } catch {
      prompt("复制" + label, value);
    }
  }
  function actionIcon(parent, kind) {
    const icon = document.createElement("img");
    icon.src = "/icons/" + kind + ".svg";
    icon.alt = "";
    icon.setAttribute("aria-hidden", "true");
    icon.className = "action-icon";
    parent.prepend(icon);
  }
  function detailMediaActions(panel, song) {
    const section = appendText(panel, "section", "", "detail-section");
    appendText(section, "h3", "视频与来源");
    const row = appendText(section, "div", "", "detail-tools");
    const link = (label, url, icon, external = false) => {
      const a = appendText(row, "a", label, "btn neutral");
      actionIcon(a, icon);
      a.href = url;
      if (external) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
    };
    const copy = (label, value, what) => {
      const btn = appendText(row, "button", label, "btn neutral");
      actionIcon(btn, "copy");
      btn.type = "button";
      btn.onclick = () => copyValue(value, what);
    };
    if (song.downloadable) link("下载成片", "/api/songs/" + song.id + "/download", "download");
    if (song.source_downloadable)
      link(
        song.source_type === "netease" ? "下载源视频" : "下载原片",
        "/api/songs/" + song.id + "/download/source",
        "download"
      );
    if (song.bvid) copy("复制 BV", song.bvid, "BV 号");
    const url = sourceUrl(song);
    if (url) {
      copy("复制链接", url, "链接");
      link("打开来源", url, "external-link", true);
    }
    if (!row.children.length) section.remove();
  }

  function songAction(parent, label, run, style = "neutral") {
    const button = appendText(parent, "button", label, "btn " + style);
    button.type = "button";
    button.onclick = () => guarded(() => run(button), button);
    return button;
  }
  function detailActions(song, isAdmin) {
    const actions = document.createElement("div");
    actions.className = "detail-actions";
    const editable = ["waiting", "ready"].includes(song.status);
    if (!isAdmin) {
      if (song.mine && editable && song.prep_state !== "aligning")
        songAction(actions, song.lyric_selected ? "更换歌词" : "选择歌词", () =>
          openLyricPicker(song.id, song.title, song.lyric_query)
        );
      if (song.mine && ["waiting", "ready", "rendering"].includes(song.status))
        songAction(actions, "取消点歌", () => cancelGuestSong(song.id), "subtle");
      if (["played", "skipped"].includes(song.status) && song.downloadable)
        songAction(
          actions,
          "↻ 再来一首",
          async () => {
            const next = await api("/api/songs/" + song.id + "/replay", "POST");
            toast("已加入队列");
            window.location.hash = "song-" + next.id;
            await refresh();
          },
          "primary"
        );
    } else {
      if (editable && song.prep_state !== "aligning")
        songAction(actions, "编辑歌词", () => editLyrics(song.id, song.title));
      if (song.status === "waiting" && song.prep_state === "unavailable")
        songAction(actions, "重试下载", async () => {
          await api("/api/admin/songs/" + song.id + "/download/retry", "POST");
          toast("已重试");
          await refresh();
        });
      if (song.status === "waiting" && !["downloading", "pending"].includes(song.prep_state)) {
        const label = appendText(actions, "label", "上传 MP4", "btn neutral detail-upload");
        const file = document.createElement("input");
        file.type = "file";
        file.accept = ".mp4,.webm,.m4v,video/mp4,video/webm";
        label.append(file);
        file.onchange = () =>
          guarded(async () => {
            if (!file.files[0]) return;
            const data = new FormData();
            data.append("file", file.files[0]);
            await api("/api/admin/songs/" + song.id + "/media", "POST", data);
            toast("已上传");
            await refresh();
          });
      }
      if (song.status === "ready" && song.prep_state === "done" && song.has_media)
        songAction(actions, "补做电视缓存", async () => {
          const result = await api("/api/admin/songs/" + song.id + "/tv-cache/retry", "POST");
          toast(
            result.status === "started"
              ? "已开始补做"
              : result.status === "not_needed"
                ? "当前原片无需缓存"
                : "电视缓存已就绪"
          );
        });
      if (song.status === "ready" && ["original", "done"].includes(song.prep_state))
        songAction(actions, song.prep_state === "done" ? "按新风格重制" : "重试字幕", async () => {
          if (song.prep_state === "done" && !confirm("按当前字幕样式重新制作？")) return;
          await api("/api/admin/songs/" + song.id + "/subtitles/retry", "POST");
          toast("已开始制作");
          await refresh();
        });
      if (
        song.status === "ready" &&
        song.has_media &&
        ["downloaded", "original", "aligning"].includes(song.prep_state) &&
        !song.skip_subtitles
      )
        songAction(actions, "原片入队", async () => {
          if (!confirm("跳过字幕，使用原片入队？")) return;
          await api("/api/admin/songs/" + song.id + "/subtitles/skip", "POST");
          toast("已加入队列");
          await refresh();
        });
      if (song.status === "ready" && song.prep_state !== "aligning")
        songAction(actions, "手工制作 KTV", async () => {
          await api("/api/admin/songs/" + song.id + "/render", "POST", { lead: leadSeconds });
          toast("制作中");
          await refresh();
        });
      if (song.status === "ready" && song.playable && !song.priority_next)
        songAction(
          actions,
          "下一首播放",
          async () => {
            await api("/api/admin/songs/" + song.id + "/pin-next", "POST");
            toast("已安排为下一首");
            await refresh();
          },
          "primary"
        );
      if (song.status === "played" && song.downloadable)
        songAction(
          actions,
          "↻ 下一首重唱",
          async () => {
            if (!confirm("安排为下一首？")) return;
            await api("/api/admin/songs/" + song.id + "/replay", "POST");
            toast("已安排为下一首");
            await refresh();
          },
          "primary"
        );
      if (["waiting", "ready", "rendering"].includes(song.status))
        songAction(
          actions,
          "取消点歌",
          async () => {
            if (!confirm("取消这首歌？")) return;
            await api("/api/admin/songs/" + song.id + "/cancel", "POST");
            window.location.hash = "";
            await refresh();
          },
          "subtle"
        );
    }
    return actions.children.length ? actions : null;
  }

  function songCoverUrl(cover) {
    if (!cover) return "";
    try {
      const url = new URL(cover);
      if (
        url.protocol === "http:" &&
        (url.hostname === "hdslb.com" || url.hostname.endsWith(".hdslb.com"))
      )
        url.protocol = "https:";
      return url.href;
    } catch {
      return "";
    }
  }
  async function refreshSongDetail() {
    const id = Number(currentRoute().slice(5));
    if (!Number.isInteger(id) || id <= 0) return;
    const song = await api("/api/songs/" + id + "/detail");
    const key = JSON.stringify([song, isAdminPath()]);
    if (detailKey === key) {
      const running = $("page-song").querySelector(".stage-current-time");
      if (running && song.prep_stage_at)
        running.textContent =
          Math.max(0, Math.floor(Date.now() / 1000 - song.prep_stage_at)) + " 秒";
      return;
    }
    detailKey = key;
    const target = $("page-song");
    const timingWasOpen = Boolean(target.querySelector(".timing-details")?.open);
    target.replaceChildren();
    const panel = appendText(target, "section", "", "panel song-detail-panel");
    const header = appendText(panel, "div", "", "detail-header");
    const coverUrl = songCoverUrl(song.cover);
    if (coverUrl) {
      const cover = appendText(header, "img", "", "detail-cover");
      cover.alt = "";
      cover.loading = "lazy";
      cover.onerror = () => {
        cover.replaceWith(appendText(header, "div", "♫", "detail-cover detail-placeholder"));
      };
      cover.src = coverUrl;
    } else appendText(header, "div", "♫", "detail-cover detail-placeholder");
    const info = appendText(header, "div", "", "detail-info");
    appendText(info, "h2", song.title);
    const metadata = appendText(info, "div", "", "detail-meta");
    appendText(metadata, "span", song.uploader || "未知作者");
    addSourceTag(metadata, song);
    appendText(
      info,
      "div",
      "点歌者 " + song.nickname + "　" + duration(song.duration),
      "detail-byline"
    );
    const state = appendText(panel, "div", "", "detail-state");
    appendText(state, "strong", songState(song));
    if (song.priority_next) appendText(state, "span", "下一首优先", "detail-note");
    if (
      !song.skip_subtitles &&
      song.prep_error &&
      ["unavailable", "original"].includes(song.prep_state)
    )
      appendText(state, "span", song.prep_error, "detail-note");
    detailMediaActions(panel, song);
    const actions = detailActions(song, isAdminPath());
    if (actions) {
      const section = appendText(panel, "section", "", "detail-section");
      appendText(section, "h3", isAdminPath() ? "歌曲管理" : "歌曲操作");
      section.append(actions);
    }
    if (isAdminPath()) {
      const names = {
        download: "下载",
        extract: "提取音频",
        kim: "人声分离",
        align: "歌词对齐",
        subtitles: "字幕生成",
        render: "烧录"
      };
      const entries = Object.entries(names).filter(([name]) => song.stage_seconds?.[name] != null);
      const running =
        song.status !== "cancelled" && ["downloading", "aligning"].includes(song.prep_state);
      if (entries.length || running || (song.prep_error && !song.skip_subtitles)) {
        const details = appendText(panel, "details", "", "timing-details");
        details.open = timingWasOpen;
        appendText(details, "summary", "制作详情");
        for (const [name, label] of entries) {
          const line = appendText(details, "div", "", "timing-row");
          appendText(line, "span", label);
          appendText(line, "span", Number(song.stage_seconds[name]).toFixed(1) + " 秒");
        }
        if (running && song.prep_stage_at) {
          const line = appendText(details, "div", "", "timing-row");
          appendText(line, "span", "当前阶段");
          appendText(
            line,
            "span",
            Math.max(0, Math.floor(Date.now() / 1000 - song.prep_stage_at)) + " 秒",
            "stage-current-time"
          );
        }
        if (song.prep_error && !song.skip_subtitles)
          appendText(details, "p", song.prep_error, "muted");
      }
    }
    if (isAdminPath() && song.status === "ready") {
      const details = appendText(panel, "details", "", "timing-details");
      appendText(details, "summary", "手工字幕参数");
      const label = appendText(details, "label", "提前秒数");
      label.htmlFor = "detail-lead-seconds";
      const lead = appendText(details, "input", "");
      lead.id = "detail-lead-seconds";
      lead.type = "number";
      lead.min = "0";
      lead.max = "5";
      lead.step = "0.1";
      lead.value = String(leadSeconds);
      lead.oninput = () => {
        leadSeconds = Number(lead.value);
      };
    }
  }

  async function editLyrics(songId, songTitle) {
    popupSongId = songId;
    await guarded(async () => {
      const data = await api(`/api/admin/songs/${songId}/lyrics`);
      $("lyrics-textarea").value = data.text;
      $("lyric-source-title").value = (songTitle || "")
        .replace(/^【[^】]*】/, "")
        .replace(/^\[[^\]]*\]/, "")
        .slice(0, 70);
      $("lyric-source-results").replaceChildren();
      $("lyrics-dialog").showModal();
    });
  }
  $("lyric-source-btn").onclick = () =>
    guarded(async () => {
      const title = $("lyric-source-title").value.trim();
      if (!title) throw new Error("请输入 VCPedia 条目名称");
      const data = await api(`/api/admin/lyrics/vcpedia?title=${encodeURIComponent(title)}`);
      const results = $("lyric-source-results");
      results.replaceChildren();
      if (!data.candidates.length)
        appendText(results, "div", "未找到歌词区块，请手动导入", "empty");
      data.candidates.forEach((c, i) => {
        const btn = appendText(
          results,
          "button",
          `使用候选 ${i + 1}  ${c.text.split("\n").length} 行  需确认版本`,
          "mini"
        );
        btn.onclick = () => {
          if ($("lyrics-textarea").value.trim() && !confirm("替换现有编辑框内容？")) return;
          $("lyrics-textarea").value = c.text;
          toast("已填入候选，请核对并保存。纯文本不能直接制作 KTV 字幕");
        };
      });
    }, $("lyric-source-btn"));
  $("textgrid-file").onchange = () =>
    guarded(async () => {
      if (!popupSongId || !$("textgrid-file").files[0]) return;
      const upload = new FormData();
      upload.append("file", $("textgrid-file").files[0]);
      const offset = Number($("alignment-offset").value);
      const result = await api(
        "/api/admin/songs/" +
          popupSongId +
          "/alignment/import?offset=" +
          encodeURIComponent(offset),
        "POST",
        upload
      );
      $("lyrics-textarea").value = result.lyrics;
      $("textgrid-file").value = "";
      toast(`已导入 ${result.report.semantic_units} 个语义词的时间戳；请复核后再生成视频`);
      await refresh();
    });
  $("lyrics-save").onclick = () =>
    guarded(async () => {
      if (popupSongId === null) return;
      await api(`/api/admin/songs/${popupSongId}/lyrics`, "POST", {
        text: $("lyrics-textarea").value
      });
      $("lyrics-dialog").close();
      toast("已保存参考歌词");
      await refresh();
    }, $("lyrics-save"));

  return { refreshSongDetail, invalidateSongDetail };
}
