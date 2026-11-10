import { $, api, guarded, toast, error, appendText, duration } from "./ui-common.js";
// A single official Bilibili player serves both P selection and lyric-side preview.
export function setupBilibiliPreview() {
  const dialog = $("bilibili-preview-dialog");
  let selectedPage = 1;
  let onChoose = null;
  let video = null;

  function showPage(page) {
    selectedPage = page;
    const bvid = encodeURIComponent(video.bvid);
    const url = `https://player.bilibili.com/player.html?bvid=${bvid}&page=${page}&high_quality=1`;
    const player = $("bilibili-preview-player");
    if (player.src !== url) player.src = url;
    $("bilibili-preview-official").href = `https://www.bilibili.com/video/${bvid}/?p=${page}`;
    for (const choice of $("bilibili-preview-parts").children)
      choice.setAttribute("aria-pressed", String(Number(choice.dataset.page) === page));
  }
  dialog.addEventListener("close", () => {
    $("bilibili-preview-player").removeAttribute("src"); // Stop audio, even after Esc.
    onChoose = null;
    video = null;
  });
  $("bilibili-preview-back").onclick = () => dialog.close();
  $("bilibili-preview-confirm").onclick = () =>
    guarded(() => onChoose?.(selectedPage), $("bilibili-preview-confirm"));

  function open(source, page, choose) {
    if (dialog.open) return;
    video = source;
    onChoose = choose;
    $("bilibili-preview-title").textContent = choose ? "选择分 P" : "预览视频";
    $("bilibili-preview-song").textContent = source.title || "";
    $("bilibili-preview-confirm").hidden = !choose;
    $("bilibili-preview-parts").hidden = !choose;
    const choices = $("bilibili-preview-parts");
    choices.replaceChildren();
    if (choose) {
      for (const part of source.pages) {
        const choice = appendText(
          choices,
          "button",
          `P${part.page}  ${part.part}`,
          "choice-card bili-part"
        );
        choice.type = "button";
        choice.dataset.page = String(part.page);
        choice.onclick = () => showPage(part.page);
      }
    }
    showPage(page);
    dialog.showModal();
  }
  return {
    choose: (source, page, onConfirm) => open(source, page, onConfirm),
    watch: (source) => open(source, source.page || 1, null),
    close: () => {
      if (dialog.open) dialog.close();
    }
  };
}

// Guest Bilibili/NetEase requests share the existing song queue.
export function setupSongRequests({ refresh, openLyricPicker, videoPreview }) {
  async function submitBilibili(info, page) {
    const result = await api("/api/songs", "POST", { bvid: info.bvid, page });
    videoPreview.close();
    $("bvid").value = "";
    toast("已点歌，请选歌词");
    await refresh();
    await openLyricPicker(result.id, result.title, result.lyric_query, {
      bvid: info.bvid,
      page,
      title: info.title
    });
  }
  $("quick-submit-btn").onclick = () =>
    guarded(async () => {
      const raw = $("bvid").value.trim();
      if (!raw) throw new Error("请输入 BV 号或视频链接");
      const info = await api(`/api/preview?bvid=${encodeURIComponent(raw)}&page=1`);
      const requested = Number(raw.match(/[?&]p=(\d+)/)?.[1]) || 1;
      const page = info.pages.some((part) => part.page === requested) ? requested : 1;
      if (info.pages.length > 1) {
        videoPreview.choose(info, page, (chosenPage) => submitBilibili(info, chosenPage));
      } else {
        await submitBilibili(info, 1);
      }
    }, $("quick-submit-btn"));
  // Source selection changes only the input panel; both sources share the same queue.
  function selectSongSource(source) {
    for (const name of ["bilibili", "netease"]) {
      const active = name === source;
      $("source-tab-" + name).setAttribute("aria-selected", String(active));
      $("source-tab-" + name).tabIndex = active ? 0 : -1;
      $("source-panel-" + name).hidden = !active;
    }
  }
  for (const source of ["bilibili", "netease"]) {
    $("source-tab-" + source).onclick = () => selectSongSource(source);
    $("source-tab-" + source).addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const next = source === "bilibili" ? "netease" : "bilibili";
      selectSongSource(next);
      $("source-tab-" + next).focus();
    });
  }
  let neteaseSearchVersion = 0;
  let neteasePage = 0;
  let neteaseLastQuery = "";
  let neteaseCanLoadMore = false;
  async function submitNeteaseSong(ref, button) {
    await guarded(async () => {
      const result = await api("/api/netease/songs", "POST", { ref });
      neteaseSearchVersion++;
      neteaseLastQuery = "";
      neteasePage = 0;
      neteaseCanLoadMore = false;
      $("netease-song-query").value = "";
      $("netease-search-results").replaceChildren();
      $("netease-search-message").textContent = "已点歌，可以继续搜索下一首。";
      toast("✓ 已加入队列，请选择歌词");
      await refresh();
      await openLyricPicker(result.id, result.title, result.lyric_query);
    }, button);
  }
  function renderNeteaseResults(songs, append = false) {
    const list = $("netease-search-results");
    if (!append) list.replaceChildren();
    for (const song of songs) {
      const item = appendText(list, "div", "", "netease-search-result");
      if (song.cover) {
        const cover = document.createElement("img");
        cover.src = song.cover;
        cover.alt = "";
        cover.loading = "lazy";
        cover.referrerPolicy = "no-referrer";
        item.append(cover);
      } else appendText(item, "span", "🎵", "netease-fallback-cover");
      const info = appendText(item, "div", "", "netease-search-result-info");
      appendText(info, "strong", song.title);
      appendText(info, "small", song.uploader + "  " + duration(song.duration));
      const choose = appendText(item, "button", "点这首", "btn neutral");
      choose.type = "button";
      choose.onclick = () => submitNeteaseSong(song.source_id, choose);
    }
  }
  async function queryNeteaseSongs() {
    const query = $("netease-song-query").value.trim();
    if (!query) throw new Error("请输入歌名、歌手或网易云分享链接");
    const version = ++neteaseSearchVersion;
    const message = $("netease-search-message");
    message.textContent = "正在查找歌曲……";
    $("netease-search-results").replaceChildren();
    try {
      const looksLikeShare = /^\d+$/.test(query) || /https?:\/\//i.test(query);
      const data = looksLikeShare
        ? await api("/api/netease/preview?ref=" + encodeURIComponent(query))
        : await api("/api/netease/search?q=" + encodeURIComponent(query));
      if (version !== neteaseSearchVersion) return;
      const songs = looksLikeShare ? [data] : data.songs;
      neteaseLastQuery = looksLikeShare ? "" : query;
      neteasePage = 0;
      neteaseCanLoadMore = !looksLikeShare && songs.length === 30;
      renderNeteaseResults(songs);
      showNeteaseMore();
      message.textContent = songs.length
        ? looksLikeShare
          ? "已识别分享歌曲，请确认歌名与歌手。"
          : "找到 " + songs.length + " 首，请选择正确版本。"
        : "没有找到匹配歌曲，可以换歌名、歌手或粘贴分享链接。";
    } catch (e) {
      if (version !== neteaseSearchVersion) return;
      message.textContent = e.message || "搜索失败";
      error(e);
    }
  }
  function showNeteaseMore() {
    const results = $("netease-search-results");
    results.querySelector(".netease-load-more")?.remove();
    if (!neteaseCanLoadMore) return;
    const button = appendText(results, "button", "更多结果", "btn netease-load-more");
    button.type = "button";
    button.onclick = () =>
      guarded(async () => {
        const version = neteaseSearchVersion;
        const next = neteasePage + 1;
        const data = await api(
          "/api/netease/search?q=" + encodeURIComponent(neteaseLastQuery) + "&page=" + next
        );
        if (version !== neteaseSearchVersion) return;
        neteasePage = next;
        neteaseCanLoadMore = data.songs.length === 30;
        button.remove();
        renderNeteaseResults(data.songs, true);
        showNeteaseMore();
      }, button);
  }
  $("netease-search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    guarded(queryNeteaseSongs, $("netease-search-btn"));
  });
  $("bvid").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("quick-submit-btn").click();
  });
}
